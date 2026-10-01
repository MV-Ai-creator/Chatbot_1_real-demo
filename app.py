import os
import csv
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types

# 1. SETUP
load_dotenv()
client = genai.Client()

app = Flask(__name__)
CORS(app)

sessions = {}

# CSV File acting as the central appointment database
APPOINTMENTS_FILE = "appointments.csv"

def init_appointments_db():
    if not os.path.exists(APPOINTMENTS_FILE):
        with open(APPOINTMENTS_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["date_time", "customer_name", "phone", "address"])

init_appointments_db()

# 2. BUSINESS KNOWLEDGE & CONFIG
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

# 3. APPOINTMENT FUNCTIONS FOR GEMINI TOOLS
def check_slot_availability(date_time: str) -> dict:
    """
    Checks if a requested appointment date and time is already booked.
    CRITICAL: date_time MUST be strictly formatted as 'YYYY-MM-DD HH:MM' (e.g., '2026-10-05 14:00').
    """
    try:
        if not os.path.exists(APPOINTMENTS_FILE):
            return {"available": True, "message": f"The slot at {date_time} is open."}
            
        clean_target = date_time.strip().lower()
        with open(APPOINTMENTS_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("date_time", "").strip().lower() == clean_target:
                    return {
                        "available": False,
                        "message": f"The slot at {date_time} is already booked. Please ask the user to select another time."
                    }
                    
        return {"available": True, "message": f"The slot at {date_time} is open."}
    except Exception as e:
        return {"available": False, "message": f"Error checking availability: {str(e)}. Please try again."}

def book_appointment(date_time: str, customer_name: str, phone: str, address: str) -> dict:
    """
    Books an appointment after verifying availability and saves it to appointments.csv.
    CRITICAL: date_time MUST be strictly formatted as 'YYYY-MM-DD HH:MM' (e.g., '2026-10-05 14:00').
    """
    try:
        # 1. Standardize phone number
        formatted_phone = clean_and_format_phone(phone)

        # 2. Check availability first
        availability = check_slot_availability(date_time)
        if not availability.get("available", True):
            return {"success": False, "message": "Slot was taken right before booking! Pick another time."}

        # 3. Save appointment directly to CSV database file
        file_exists = os.path.exists(APPOINTMENTS_FILE)
        with open(APPOINTMENTS_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date_time", "customer_name", "phone", "address"])
            writer.writerow([date_time.strip(), customer_name.strip(), formatted_phone, address.strip()])

        # 4. Return success confirmation
        return {
            "success": True, 
            "message": f"Successfully booked appointment for {customer_name} at {date_time}. Saved to database."
        }
    except Exception as e:
        return {"success": False, "message": f"Database error while booking: {str(e)}"}

# List of tool functions provided to Gemini
bot_tools = [check_slot_availability, book_appointment]

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

        # FORCE ILLINOIS CENTRAL TIME ZONE
        il_timezone = ZoneInfo("America/Chicago")
        current_time_str = datetime.now(il_timezone).strftime("%A, %B %d, %Y at %I:%M %p")

        # Initialize chat session if needed
        if session_id not in sessions:
            sessions[session_id] = client.chats.create(
                model="gemini-3.1-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=(
                        f"You are a professional AI receptionist for {BUSINESS_NAME}.\n\n"
                        f"Knowledge Base:\n{BUSINESS_KNOWLEDGE}\n\n"
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
        chat_session = sessions[session_id]
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

        # CREATE SEPARATE FOLDER FOR THIS SPECIFIC CLIENT
        client_folder = os.path.join("clients", session_id)
        os.makedirs(client_folder, exist_ok=True)

        # SAVE MESSAGES TO A FILE INSIDE THAT CLIENT'S FOLDER (using Illinois time)
        client_file = os.path.join(client_folder, "chat_history.txt")
        with open(client_file, mode='a', encoding='utf-8') as f:
            f.write(f"[{datetime.now(il_timezone).strftime('%Y-%m-%d %H:%M:%S')}] User: {user_message}\n")
            f.write(f"[{datetime.now(il_timezone).strftime('%Y-%m-%d %H:%M:%S')}] Bot: {response.text}\n\n")

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