import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client, Client

# 1. SETUP & INITIALIZATION
load_dotenv()

# Initialize Gemini Client
gemini_client = genai.Client()

# Initialize Supabase Client
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY in .env file")

supabase: Client = create_client(supabase_url, supabase_key)

app = Flask(__name__)
CORS(app)

sessions = {}

# 2. BUSINESS KNOWLEDGE & CONFIG (DEFAULT / FALLBACK)
BUSINESS_NAME = "Apex Plumbing Co."
BUSINESS_PHONE = "(555) 123-4567"

BUSINESS_KNOWLEDGE = f"""
Operating Hours: Mon-Fri 8 AM - 6 PM. (24/7 Emergency dispatch available).
Services Offered: Drain cleaning, pipe leak repairs, water heater replacements.
Pricing Policy: We charge an $89 diagnostic fee to send a truck out.
Business Contact Phone: {BUSINESS_PHONE}
Frequently Asked Questions:
- Do you offer financing? Yes, for any job over $1,000.
- Are you insured? Yes, fully licensed, bonded, and insured.
"""

# PHONE FORMATTER: TAKES ANY STRING AND TURNS IT INTO XXX-XXX-XXXX
def clean_and_format_phone(raw_phone: str) -> str:
    digits = re.sub(r'\D', '', str(raw_phone))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return str(raw_phone).strip()

# 3. APPOINTMENT FUNCTIONS FOR GEMINI TOOLS (SUPABASE BACKED)
def check_slot_availability(date_time: str, tenant_id: str) -> dict:
    """
    Checks if a requested appointment date and time is already booked in Supabase.
    CRITICAL: date_time MUST be strictly formatted as 'YYYY-MM-DD HH:MM' (e.g., '2026-10-05 14:00').
    """
    try:
        clean_target = date_time.strip()
        response = supabase.table("appointments") \
            .select("id") \
            .eq("tenant_id", tenant_id) \
            .eq("date_time", clean_target) \
            .execute()

        if response.data and len(response.data) > 0:
            return {
                "available": False,
                "message": f"The slot at {date_time} is already booked. Please ask the user to select another time."
            }

        return {"available": True, "message": f"The slot at {date_time} is open."}
    except Exception as e:
        return {"available": False, "message": f"Error checking availability: {str(e)}. Please try again."}

def book_appointment(date_time: str, customer_name: str, phone: str, address: str, tenant_id: str) -> dict:
    """
    Books an appointment after verifying availability and saves it to Supabase.
    CRITICAL: date_time MUST be strictly formatted as 'YYYY-MM-DD HH:MM' (e.g., '2026-10-05 14:00').
    """
    try:
        # 1. Standardize phone number
        formatted_phone = clean_and_format_phone(phone)

        # 2. Check availability first
        availability = check_slot_availability(date_time, tenant_id)
        if not availability.get("available", True):
            return {"success": False, "message": "Slot was taken right before booking! Pick another time."}

        # 3. Save appointment directly to Supabase database
        il_timezone = ZoneInfo("America/Chicago")
        appointment_payload = {
            "tenant_id": tenant_id,
            "date_time": date_time.strip(),
            "customer_name": customer_name.strip(),
            "phone": formatted_phone,
            "address": address.strip(),
            "created_at": datetime.now(il_timezone).isoformat()
        }

        supabase.table("appointments").insert(appointment_payload).execute()

        # 4. Return success confirmation
        return {
            "success": True, 
            "message": f"Successfully booked appointment for {customer_name} at {date_time}. Saved to database."
        }
    except Exception as e:
        return {"success": False, "message": f"Database error while booking: {str(e)}"}

# 4. ROUTES
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({"error": "Missing message parameter"}), 400

        user_message = data.get('message', '')
        session_id = data.get('session_id', 'client_1')
        tenant_id = data.get('tenant_id', 'demo_client_1')

        # FORCE ILLINOIS CENTRAL TIME ZONE
        il_timezone = ZoneInfo("America/Chicago")
        current_time_str = datetime.now(il_timezone).strftime("%A, %B %d, %Y at %I:%M %p")

        # Fetch Tenant metadata from Supabase, or fall back to default business info
        tenant_res = supabase.table("tenants").select("*").eq("id", tenant_id).execute()
        
        if tenant_res.data and len(tenant_res.data) > 0:
            tenant_info = tenant_res.data[0]
            biz_name = tenant_info.get("business_name", BUSINESS_NAME)
            biz_phone = tenant_info.get("business_phone", BUSINESS_PHONE)
            biz_knowledge = tenant_info.get("knowledge_base", BUSINESS_KNOWLEDGE)
        else:
            biz_name = BUSINESS_NAME
            biz_phone = BUSINESS_PHONE
            biz_knowledge = BUSINESS_KNOWLEDGE

        # Tool wrappers to pass the dynamic tenant_id
        def check_slot_tool(date_time: str) -> dict:
            return check_slot_availability(date_time, tenant_id)

        def book_appointment_tool(date_time: str, customer_name: str, phone: str, address: str) -> dict:
            return book_appointment(date_time, customer_name, phone, address, tenant_id)

        bot_tools = [check_slot_tool, book_appointment_tool]

        # Initialize chat session if needed
        session_key = f"{tenant_id}_{session_id}"
        if session_key not in sessions:
            sessions[session_key] = gemini_client.chats.create(
                model="gemini-3.1-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=(
                        f"You are a professional AI receptionist for {biz_name}.\n\n"
                        f"Knowledge Base:\n{biz_knowledge}\n\n"
                        f"CRITICAL SCHEDULING INSTRUCTIONS:\n"
                        f"- Always look at the current real-time timestamp provided in user prompts.\n"
                        f"- When checking availability or booking, you MUST convert any relative date/time "
                        f"(like 'tomorrow at 2pm' or 'Friday morning') into strict standard format: 'YYYY-MM-DD HH:MM' (24-hour time).\n"
                        f"- Example: If today is Wednesday, Sep 30, 2026 and user wants tomorrow at 2 PM, format it as '2026-10-01 14:00'.\n\n"
                        f"When a user wants to book an appointment:\n"
                        f"1. Collect their desired date and time, full name, phone number, and address.\n"
                        f"2. Call `check_slot_availability` with the exact 'YYYY-MM-DD HH:MM' string BEFORE confirming.\n"
                        f"3. Call `book_appointment` with the exact 'YYYY-MM-DD HH:MM' string to lock in the reservation and save it to the database.\n"
                        f"4. If already booked, politely let them know and offer alternative times."
                    ),
                    tools=bot_tools
                )
            )

        # Send message with automatic retry logic for temporary 503 server overloads
        chat_session = sessions[session_key]
        prompt_with_time = f"[Current Real-Time (Central Time): {current_time_str}] {user_message}"
        
        response = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = chat_session.send_message(prompt_with_time)
                break
            except Exception as api_err:
                if ("503" in str(api_err) or "unavailable" in str(api_err).lower()) and attempt < max_retries - 1:
                    print(f"API high demand encountered. Retrying in 2 seconds... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(2)
                else:
                    raise api_err

        # SAVE MESSAGES DIRECTLY TO SUPABASE CHAT_LOGS TABLE (using Illinois time)
        log_payload = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_message": user_message,
            "bot_response": response.text,
            "created_at": datetime.now(il_timezone).isoformat()
        }
        supabase.table("chat_logs").insert(log_payload).execute()

        return jsonify({"response": response.text})

    except Exception as e:
        print(f"Server Error: {e}")
        
        # GRACEFUL FALLBACK: Send friendly message to user with business phone instead of crashing
        fallback_response = (
            f"I am so sorry, but I am experiencing a temporary connection hiccup right now. "
            f"If you need immediate assistance or want to finish booking your appointment, "
            f"please give us a call directly at {BUSINESS_PHONE}!"
        )
        return jsonify({"response": fallback_response})

if __name__ == '__main__':
    app.run(port=5000, debug=True)