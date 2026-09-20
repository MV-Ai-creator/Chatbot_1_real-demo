import os
import csv
from datetime import datetime
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

# 2. BUSINESS KNOWLEDGE
BUSINESS_NAME = "Apex Plumbing Co."
BUSINESS_KNOWLEDGE = """
Operating Hours: Mon-Fri 8 AM - 6 PM. (24/7 Emergency dispatch available).
Services Offered: Drain cleaning, pipe leak repairs, water heater replacements.
Pricing Policy: We charge an $89 diagnostic fee to send a truck out.
Frequently Asked Questions:
- Do you offer financing? Yes, for any job over $1,000.
- Are you insured? Yes, fully licensed, bonded, and insured.
"""

# 3. APPOINTMENT FUNCTIONS FOR GEMINI TOOLS
def check_slot_availability(date_time: str) -> dict:
    """
    Checks if a requested appointment date and time is already booked.
    Format for date_time should be standard, e.g., 'YYYY-MM-DD HH:MM' or '2026-10-15 16:00'.
    """
    if not os.path.exists(APPOINTMENTS_FILE):
        return {"available": True}
        
    with open(APPOINTMENTS_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["date_time"].strip().lower() == date_time.strip().lower():
                return {
                    "available": False,
                    "message": f"The slot at {date_time} is already booked. Please ask the user to select another time."
                }
                
    return {"available": True, "message": f"The slot at {date_time} is open."}

def book_appointment(date_time: str, customer_name: str, phone: str, address: str) -> dict:
    """
    Books an appointment after verifying availability.
    """
    # Double-check availability prior to saving
    availability = check_slot_availability(date_time)
    if not availability["available"]:
        return {"success": False, "message": "Slot was taken right before booking! Pick another time."}

    with open(APPOINTMENTS_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([date_time.strip(), customer_name, phone, address])

    return {"success": True, "message": f"Successfully booked for {customer_name} at {date_time}."}

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

        # Initialize chat session if needed
        if session_id not in sessions:
            sessions[session_id] = client.chats.create(
                model="gemini-3.1-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=f"You are a professional AI receptionist for {BUSINESS_NAME}.\n\n"
                                       f"Knowledge Base:\n{BUSINESS_KNOWLEDGE}\n\n"
                                       f"When a user wants to book an appointment:\n"
                                       f"1. Ask for their desired date and time, name, phone number, and address.\n"
                                       f"2. Check slot availability using `check_slot_availability` BEFORE confirming.\n"
                                       f"3. If available, call `book_appointment` to lock in the reservation.\n"
                                       f"4. If already booked, politely let them know and offer alternative times.",
                    tools=bot_tools
                )
            )

        # Get response from Gemini
        chat_session = sessions[session_id]
        response = chat_session.send_message(user_message)

        # CREATE SEPARATE FOLDER FOR THIS SPECIFIC CLIENT
        client_folder = os.path.join("clients", session_id)
        os.makedirs(client_folder, exist_ok=True)

        # SAVE MESSAGES TO A FILE INSIDE THAT CLIENT'S FOLDER
        client_file = os.path.join(client_folder, "chat_history.txt")
        with open(client_file, mode='a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] User: {user_message}\n")
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bot: {response.text}\n\n")

        return jsonify({"response": response.text})

    except Exception as e:
        print(f"Server Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)