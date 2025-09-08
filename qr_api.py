from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import qrcode
import random
import string
import base64
from io import BytesIO
import time
from datetime import datetime, timedelta
import pandas as pd
import os
from pymongo import MongoClient
from bson import ObjectId
import json
from dotenv import load_dotenv
import pyotp

# Load environment variables
load_dotenv()

app = Flask(__name__)

# CORS Configuration - More permissive for Vercel
CORS(app, 
     origins=["*"],  # Allow all origins
     methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
     allow_headers=["*"],  # Allow all headers
     supports_credentials=False,  # Change to False for broader compatibility
     expose_headers=["*"]  # Expose all headers
)

@app.after_request
def after_request(response):
    # Remove restrictive CORS headers and make them more permissive
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = '*'
    response.headers['Access-Control-Allow-Methods'] = '*'
    response.headers['Access-Control-Allow-Credentials'] = 'false'
    response.headers['Access-Control-Max-Age'] = '86400'
    
    # Remove any caching that might interfere
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

# Configuration from environment variables
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb+srv://megh:marco@vicecluster.4wafcsu.mongodb.net/?retryWrites=true&w=majority&appName=vicecluster')
DATABASE_NAME = os.getenv('DATABASE_NAME', 'kl_university_attendance')
STUDENTS_COLLECTION = os.getenv('STUDENTS_COLLECTION', 'students')
ATTENDANCE_COLLECTION = os.getenv('ATTENDANCE_COLLECTION', 'attendance_records')
QR_SESSIONS_COLLECTION = os.getenv('QR_SESSIONS_COLLECTION', 'qr_sessions')
FACULTY_COLLECTION = os.getenv('FACULTY_COLLECTION', 'faculty')
PORT = int(os.getenv('PORT', 5000))

# Initialize MongoDB client
try:
    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]
    students_collection = db[STUDENTS_COLLECTION]
    attendance_collection = db[ATTENDANCE_COLLECTION]
    qr_sessions_collection = db[QR_SESSIONS_COLLECTION]
    faculty_collection = db[FACULTY_COLLECTION]
    
    # Test connection
    client.admin.command('ping')
    print("✅ Successfully connected to MongoDB Atlas!")
    
except Exception as e:
    print(f"❌ Failed to connect to MongoDB: {e}")
    client = None

# Configuration
QR_VALIDITY_SECONDS = 30 # Changed from 30 to 3 seconds
QR_AUTO_REFRESH_INTERVAL = 5  # Auto-generate new QR every 3 seconds

# Global variable to track current QR session
current_qr_session = None
qr_generation_thread = None

import threading
import time

KEEP_PREVIOUS_ACTIVE = os.getenv("KEEP_PREVIOUS_ACTIVE", "1") == "1"
ACCEPT_ROTATED_WITHIN_EXPIRY = os.getenv("ACCEPT_ROTATED_WITHIN_EXPIRY", "1") == "1"
KEEP_ATTENDANCE_ON_EXPIRE = os.getenv("KEEP_ATTENDANCE_ON_EXPIRE", "1") == "1"
ATTENDANCE_RETENTION_DAYS = int(os.getenv("ATTENDANCE_RETENTION_DAYS", "90"))

def auto_generate_qr():
    """Background thread to automatically generate new QR codes every QR_AUTO_REFRESH_INTERVAL seconds"""
    global current_qr_session
    while True:
        try:
            if not client:
                print("❌ Database not connected, skipping auto QR generation")
                time.sleep(QR_AUTO_REFRESH_INTERVAL)
                continue

            # Only deactivate the immediately previous session (NOT all) if we do NOT keep previous active
            if not KEEP_PREVIOUS_ACTIVE and current_qr_session:
                qr_sessions_collection.update_one(
                    {"_id": current_qr_session["_id"], "is_active": True},
                    {"$set": {"is_active": False, "terminated_at": datetime.now(), "auto_terminated": True}}
                )

            cleanup_expired_sessions_and_data()

            qr_data = generate_random_data()
            qr_image = generate_qr_image(qr_data)
            if qr_image:
                now = datetime.now()
                new_session = {
                    "qr_code": qr_data,
                    "created_at": now,
                    "expires_at": now + timedelta(seconds=QR_VALIDITY_SECONDS),
                    "is_active": True,
                    "used_by": [],
                    "session_name": f"AutoSession_{now.strftime('%H%M%S')}",
                    "created_by": "AUTO_GENERATOR",
                    "auto_generated": True,
                    "qr_image": qr_image
                }
                ins = qr_sessions_collection.insert_one(new_session)
                new_session["_id"] = ins.inserted_id
                current_qr_session = new_session
                print(f"🔄 NEW QR {qr_data} valid {QR_VALIDITY_SECONDS}s keep_prev={KEEP_PREVIOUS_ACTIVE}")
        except Exception as e:
            print(f"❌ Error in auto_generate_qr: {e}")
        time.sleep(QR_AUTO_REFRESH_INTERVAL)

def start_auto_qr_generation():
    """Start the background QR generation thread"""
    global qr_generation_thread
    
    if qr_generation_thread is None or not qr_generation_thread.is_alive():
        qr_generation_thread = threading.Thread(target=auto_generate_qr, daemon=True)
        qr_generation_thread.start()
        print("🚀 Auto QR generation started (every 3 seconds)")

def initialize_database():
    """Initialize the database with student records"""
    try:
        if not client:
            print("❌ MongoDB not connected. Cannot initialize database.")
            return False
            
        # Check if students already exist
        if students_collection.count_documents({}) > 0:
            print("✅ Students already exist in database")
            return True
        
        print("🔄 Initializing database with student records...")
        
        # Create student records (2410080001 to 2410080085)
        students = []
        for i in range(1, 86):
            student_id = f"2410080{i:03d}"
            student = {
                "student_id": student_id,
                "name": f"Student {i:03d}",
                "department": "AIDS",
                "year": "2024",
                "email": f"student{i:03d}@kluniversity.in",
                "phone": f"9876543{i:03d}",
                "created_at": datetime.now(),
                "is_active": True
            }
            students.append(student)
        
        # Insert all students
        result = students_collection.insert_many(students)
        print(f"✅ Inserted {len(result.inserted_ids)} student records")
        
        # Create indexes for better performance
        students_collection.create_index("student_id", unique=True)
        attendance_collection.create_index([("student_id", 1), ("session_date", 1)])
        qr_sessions_collection.create_index("expires_at")
        
        print("✅ Database indexes created")
        return True
        
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        return False

def generate_random_data(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_qr_image(data):
    """Generate QR code image"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        print(f"Error generating QR image: {e}")
        return None

def cleanup_expired_qr_codes():
    """Legacy function - now calls the enhanced cleanup"""
    return cleanup_expired_sessions_and_data()

def cleanup_expired_sessions_and_data():
    """Handle expired QR sessions. By default keep attendance; optionally purge very old data."""
    try:
        if not client:
            return 0

        now = datetime.now()

        # 1. Mark newly expired sessions as inactive (do not delete attendance)
        result_mark = qr_sessions_collection.update_many(
            {
                "expires_at": {"$lt": now},
                "is_active": True
            },
            {
                "$set": {
                    "is_active": False,
                    "expired_at": now
                }
            }
        )

        deleted_sessions = 0
        deleted_attendance = 0

        # 2. (Optional) Hard delete sessions (and maybe attendance) older than retention window
        retention_cutoff = now - timedelta(days=ATTENDANCE_RETENTION_DAYS)
        old_sessions = list(qr_sessions_collection.find(
            {"expires_at": {"$lt": retention_cutoff}}
        ).limit(500))

        if old_sessions:
            old_session_ids = [s["_id"] for s in old_sessions]

            if KEEP_ATTENDANCE_ON_EXPIRE:
                # Only delete the old session docs; keep attendance history
                del_sess_res = qr_sessions_collection.delete_many({"_id": {"$in": old_session_ids}})
                deleted_sessions = del_sess_res.deleted_count
            else:
                # Delete both sessions and their attendance
                del_att_res = attendance_collection.delete_many({"qr_session_id": {"$in": old_session_ids}})
                del_sess_res = qr_sessions_collection.delete_many({"_id": {"$in": old_session_ids}})
                deleted_sessions = del_sess_res.deleted_count
                deleted_attendance = del_att_res.deleted_count

            if deleted_sessions or deleted_attendance or result_mark.modified_count:
                print(f"🧹 CLEANUP: expired->inactive={result_mark.modified_count}, "
                      f"old_sessions_deleted={deleted_sessions}, "
                      f"old_attendance_deleted={deleted_attendance}, "
                      f"keep_attendance={KEEP_ATTENDANCE_ON_EXPIRE}")

        return result_mark.modified_count + deleted_sessions

    except Exception as e:
        print(f"❌ Error in cleanup_expired_sessions_and_data: {e}")
        return 0

# Health check endpoint for Render
@app.route('/')
@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'KL University Attendance API',
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat(),
        'mongodb_connected': client is not None
    })

# API Routes
@app.route('/qr')
def get_qr():
    """Get the current auto-generated QR code"""
    try:
        global current_qr_session
        
        if not client:
            return jsonify({"error": "Database not connected"}), 500
        
        # Start auto-generation if not running
        start_auto_qr_generation()
        
        # If no current session, wait a moment for auto-generation
        if not current_qr_session:
            time.sleep(0.5)  # Brief wait for auto-generation to start
        
        # Get the most recent active QR session
        try:
            active_qr = qr_sessions_collection.find_one({
                "is_active": True,
                "expires_at": {"$gt": datetime.now()}
            }, sort=[("created_at", -1)])
        except Exception as db_error:
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        if not active_qr:
            return jsonify({
                "error": "No active QR code available",
                "message": "Auto-generation starting, please try again in a moment"
            }), 503
        
        current_time = datetime.now()
        time_remaining = (active_qr['expires_at'] - current_time).total_seconds()
        
        return jsonify({
            "data": active_qr['qr_code'],
            "image": active_qr.get('qr_image', ''),
            "timestamp": current_time.isoformat(),
            "expires_at": active_qr['expires_at'].isoformat(),
            "expires_in": max(0, int(time_remaining)),
            "session_id": str(active_qr['_id']),
            "session_name": active_qr["session_name"],
            "auto_generated": True,
            "refresh_interval": QR_AUTO_REFRESH_INTERVAL,
            "message": f"QR auto-refreshes every {QR_AUTO_REFRESH_INTERVAL} seconds"
        })
        
    except Exception as e:
        print(f"❌ Error in get_qr: {e}")
        return jsonify({"error": str(e)}, 500)

@app.route('/validate', methods=['POST', 'OPTIONS'])
def validate_qr():
    """Validate QR code and mark attendance"""
    if request.method == 'OPTIONS':
        return jsonify({'status': 'OK'}), 200
        
    try:
        if not client:
            return jsonify({
                'valid': False,
                'message': 'Database not connected'
            }), 500
            
        cleanup_expired_qr_codes()
        
        # Better JSON parsing with error handling
        try:
            data = request.get_json()
            if not data:
                return jsonify({
                    'valid': False,
                    'message': 'No JSON data provided'
                }), 400
        except Exception as json_error:
            return jsonify({
                'valid': False,
                'message': f'Invalid JSON format: {str(json_error)}'
            }), 400
            
        qr_code = data.get('qr_code', '').strip()
        student_id = data.get('student_id', '').strip()
        student_name = data.get('student_name', '').strip()
        
        print(f"🔍 Validation request: QR={qr_code}, Student={student_id}")
        
        # Validate input
        if not qr_code:
            return jsonify({
                'valid': False,
                'message': 'QR code is required'
            }), 400
            
        if not student_id:
            return jsonify({
                'valid': False,
                'message': 'Student ID is required'
            }), 400
        
        # Check if student exists in database
        try:
            student = students_collection.find_one({"student_id": student_id})
            if not student:
                return jsonify({
                    'valid': False,
                    'message': f'Student ID {student_id} not found in database'
                }), 400
        except Exception as db_error:
            print(f"❌ Database error finding student: {db_error}")
            return jsonify({
                'valid': False,
                'message': 'Database error while finding student'
            }), 500
        
        # Check if QR code exists and is valid
        current_time = datetime.now()
        try:
            # Fetch session regardless of active flag
            qr_session = qr_sessions_collection.find_one({"qr_code": qr_code})
        except Exception as qr_error:
            print(f"❌ Database error finding QR session: {qr_error}")
            return jsonify({'valid': False,'message': 'Database error while validating QR code'}), 500

        if not qr_session:
            return jsonify({'valid': False,'message': 'Invalid QR code'}), 400

        expired = qr_session['expires_at'] <= current_time
        rotated = (not qr_session.get('is_active', True)) and not expired

        if expired:
            return jsonify({'valid': False,'message': 'QR code expired. Scan a new one.'}), 400

        if rotated and not ACCEPT_ROTATED_WITHIN_EXPIRY:
            return jsonify({'valid': False,'message': 'QR code rotated. Scan the latest QR now displayed.'}), 400
        # If rotated but ACCEPT_ROTATED_WITHIN_EXPIRY=True, continue as valid
        
        # Check if student already marked attendance with this QR
        used_by_list = qr_session.get('used_by', [])
        if student_id in used_by_list:
            return jsonify({
                'valid': False,
                'message': 'You have already marked attendance with this QR code'
            }), 400
        
        # Check if student already marked attendance today
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            existing_attendance = attendance_collection.find_one({
                "student_id": student_id,
                "session_date": today_start
            })
        except Exception as attendance_error:
            print(f"❌ Database error checking existing attendance: {attendance_error}")
            return jsonify({
                'valid': False,
                'message': 'Database error while checking attendance'
            }), 500
        
        if existing_attendance:
            attendance_time = existing_attendance.get('marked_at', 'Unknown time')
            return jsonify({
                'valid': False,
                'message': f'You have already marked attendance today at {attendance_time.strftime("%H:%M:%S") if hasattr(attendance_time, "strftime") else attendance_time}'
            }), 400
        
        # Mark attendance
        attendance_record = {
            "student_id": student_id,
            "student_name": student.get('name', student_name),
            "department": student.get('department', 'AIDS'),
            "year": student.get('year', '2024'),
            "qr_code": qr_code,
            "qr_session_id": qr_session['_id'],
            "marked_at": current_time,
            "session_date": today_start,
            "status": "present",
            "ip_address": request.remote_addr or 'Unknown',
            "user_agent": request.headers.get('User-Agent', 'Unknown')
        }
        
        try:
            # Insert attendance record
            attendance_result = attendance_collection.insert_one(attendance_record)
            print(f"✅ INSERTED attendance _id={attendance_result.inserted_id} student={student_id} qr={qr_code}")
            
            # Update QR session to mark it as used by this student
            qr_sessions_collection.update_one(
                {"_id": qr_session['_id']},
                {"$push": {"used_by": student_id}}
            )
            
            print(f"✅ Attendance marked: {student_id} - {student.get('name')}")
            
            return jsonify({
                'valid': True,
                'message': f'Attendance marked successfully for {student.get("name", student_name)}!',
                'student_name': student.get('name', student_name),
                'student_id': student_id,
                'timestamp': current_time.isoformat(),
                'attendance_id': str(attendance_result.inserted_id)
            })
            
        except Exception as insert_error:
            print(f"❌ Database error inserting attendance: {insert_error}")
            # Add explicit failure log
            return jsonify({
                'valid': False,
                'message': 'Failed to save attendance record'
            }), 500
        
    except Exception as e:
        print(f"❌ Unexpected error in validate_qr: {e}")
        return jsonify({
            'valid': False,
            'message': f'Server error: Please try again later'
        }), 500

# Also fix the download_excel function for better date handling
@app.route('/download/excel')
def download_excel():
    """Download attendance report as Excel"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
            
        # Get date range (today or specific date)
        date_filter = request.args.get('date')
        if date_filter:
            try:
                # Handle different date formats
                if 'T' in date_filter:
                    target_date = datetime.fromisoformat(date_filter.replace('Z', '+00:00'))
                else:
                    target_date = datetime.strptime(date_filter, '%Y-%m-%d')
                start_date = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            except ValueError as date_error:
                return jsonify({'error': f'Invalid date format: {date_filter}. Use YYYY-MM-DD'}), 400
        else:
            # Today only
            start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        end_date = start_date + timedelta(days=1)
        
        # Get all students
        students = list(students_collection.find({}).sort("student_id", 1))
        
        # Get attendance records for the date range
        attendance_records = list(attendance_collection.find({
            "session_date": start_date
        }))
        
        # Create attendance lookup
        attendance_lookup = {}
        for record in attendance_records:
            attendance_lookup[record['student_id']] = record
        
        # Prepare Excel data
        excel_data = []
        for student in students:
            row = {
                'Student_ID': student['student_id'],
                'Name': student['name'],
                'Department': student['department'],
                'Year': student['year'],
                'Email': student.get('email', ''),
                'Phone': student.get('phone', '')
            }
            
            # Add attendance status
            if student['student_id'] in attendance_lookup:
                attendance_record = attendance_lookup[student['student_id']]
                row['Attendance_Status'] = 'Present'
                row['Attendance_Time'] = attendance_record['marked_at'].strftime('%H:%M:%S')
                row['QR_Code_Used'] = attendance_record['qr_code']
            else:
                row['Attendance_Status'] = 'Absent'
                row['Attendance_Time'] = ''
                row['QR_Code_Used'] = ''
            
            excel_data.append(row)
        
        # Create DataFrame and Excel file
        df = pd.DataFrame(excel_data)
        
        # Create Excel file in memory
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Attendance Report', index=False)
        
        excel_buffer.seek(0)
        
        # Generate filename
        filename = f"Attendance_Report_{start_date.strftime('%Y%m%d')}.xlsx"
        
        return send_file(
            excel_buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        print(f"❌ Error in download_excel: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to generate Excel report'
        }), 500

@app.route('/download/session/<session_id>')
def download_session_excel(session_id):
    """Download attendance report for a specific QR session - triggers auto cleanup"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        # AUTO-CLEANUP: Remove expired sessions when downloading
        print("🗑️ Performing auto-cleanup during download...")
        cleanup_expired_sessions_and_data()
            
        # Find the QR session
        try:
            qr_session = qr_sessions_collection.find_one({"_id": ObjectId(session_id)})
        except:
            return jsonify({'error': 'Invalid session ID format'}), 400
            
        if not qr_session:
            return jsonify({'error': 'QR session not found'}), 404
        
        # Get attendance records for this specific session
        attendance_records = list(attendance_collection.find({
            "qr_session_id": ObjectId(session_id)
        }))
        
        if not attendance_records:
            return jsonify({'error': 'No attendance records found for this session'}), 404
        
        # Prepare Excel data for this session only
        excel_data = []
        for record in attendance_records:
            row = {
                'Session_ID': session_id,
                'QR_Code': record['qr_code'],
                'Student_ID': record['student_id'],
                'Student_Name': record['student_name'],
                'Department': record['department'],
                'Year': record['year'],
                'Attendance_Time': record['marked_at'].strftime('%Y-%m-%d %H:%M:%S'),
                'Status': 'Present',
                'IP_Address': record.get('ip_address', ''),
                'User_Agent': record.get('user_agent', '')[:50] + '...' if len(record.get('user_agent', '')) > 50 else record.get('user_agent', '')
            }
            excel_data.append(row)
        
        # Create DataFrame and Excel file
        df = pd.DataFrame(excel_data)
        
        # Create Excel file in memory
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name=f'Session {session_id[:8]}', index=False)
            
            # Add summary sheet
            summary_data = [{
                'Session_ID': session_id,
                'QR_Code': qr_session['qr_code'],
                'Session_Created': qr_session['created_at'].strftime('%Y-%m-%d %H:%M:%S'),
                'Session_Expired': qr_session['expires_at'].strftime('%Y-%m-%d %H:%M:%S'),
                'Total_Attendees': len(attendance_records),
                'Students_Present': ', '.join([r['student_id'] for r in attendance_records]),
                'Download_Time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'Auto_Cleanup_Performed': 'Yes'
            }]
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, sheet_name='Session Summary', index=False)
        
        excel_buffer.seek(0)
        
        # Generate unique filename with timestamp
        session_time = qr_session['created_at'].strftime('%Y%m%d_%H%M%S')
        filename = f"Attendance_Session_{session_time}_{session_id[:8]}.xlsx"
        
        print(f"📊 Session download complete: {filename}")
        print(f"🗑️ Expired data automatically cleaned up")
        
        return send_file(
            excel_buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        print(f"❌ Error in download_session_excel: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to generate session Excel report'
        }), 500

@app.route('/download/latest-session')
def download_latest_session():
    """Download attendance report for the most recent QR session - triggers auto cleanup"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        # AUTO-CLEANUP: Remove expired sessions when downloading latest
        print("🗑️ Performing auto-cleanup during latest download...")
        cleanup_expired_sessions_and_data()
        
        # Find the most recent session that has attendance records
        latest_attendance = attendance_collection.find().sort("marked_at", -1).limit(1)
        latest_record = list(latest_attendance)
        
        if not latest_record:
            return jsonify({'error': 'No attendance records found'}), 404
        
        session_id = str(latest_record[0]['qr_session_id'])
        
        # Redirect to session-specific download
        from flask import redirect, url_for
        return redirect(url_for('download_session_excel', session_id=session_id))
        
    except Exception as e:
        print(f"❌ Error in download_latest_session: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to find latest session'
        }), 500

@app.route('/sessions/active')
def get_active_sessions():
    """Get list of active QR sessions with attendance counts"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        # Get all sessions from today
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        
        sessions = list(qr_sessions_collection.find({
            "created_at": {"$gte": today_start, "$lt": today_end}
        }).sort("created_at", -1))
        
        session_data = []
        for session in sessions:
            # Count attendance for this session
            attendance_count = attendance_collection.count_documents({
                "qr_session_id": session['_id']
            })
            
            # Get attendee list
            attendees = list(attendance_collection.find({
                "qr_session_id": session['_id']
            }, {"student_id": 1, "student_name": 1, "marked_at": 1}))
            
            session_info = {
                'session_id': str(session['_id']),
                'qr_code': session['qr_code'],
                'created_at': session['created_at'].isoformat(),
                'expires_at': session['expires_at'].isoformat(),
                'is_expired': session['expires_at'] < datetime.now(),
                'attendance_count': attendance_count,
                'attendees': [{'student_id': a['student_id'], 'student_name': a['student_name'], 'marked_at': a['marked_at'].isoformat()} for a in attendees],
                'download_url': f"/download/session/{str(session['_id'])}"
            }
            session_data.append(session_info)
        
        return jsonify({
            'sessions': session_data,
            'total_sessions': len(session_data),
            'total_attendees_today': sum(s['attendance_count'] for s in session_data)
        })
        
    except Exception as e:
        print(f"❌ Error in get_active_sessions: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to fetch sessions'
        }), 500

@app.route('/sessions/by-date/<date>')
def get_sessions_by_date(date):
    """Get sessions for a specific date"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        # Parse date
        try:
            target_date = datetime.strptime(date, '%Y-%m-%d')
            start_date = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + timedelta(days=1)
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        sessions = list(qr_sessions_collection.find({
            "created_at": {"$gte": start_date, "$lt": end_date}
        }).sort("created_at", -1))
        
        session_data = []
        for session in sessions:
            attendance_count = attendance_collection.count_documents({
                "qr_session_id": session['_id']
            })
            
            attendees = list(attendance_collection.find({
                "qr_session_id": session['_id']
            }, {"student_id": 1, "student_name": 1, "marked_at": 1}))
            
            session_info = {
                'session_id': str(session['_id']),
                'qr_code': session['qr_code'],
                'created_at': session['created_at'].isoformat(),
                'expires_at': session['expires_at'].isoformat(),
                'is_expired': session['expires_at'] < datetime.now(),
                'attendance_count': attendance_count,
                'attendees': [{'student_id': a['student_id'], 'student_name': a['student_name'], 'marked_at': a['marked_at'].isoformat()} for a in attendees],
                'download_url': f"/download/session/{str(session['_id'])}"
            }
            session_data.append(session_info)
        
        return jsonify({
            'date': date,
            'sessions': session_data,
            'total_sessions': len(session_data),
            'total_attendees': sum(s['attendance_count'] for s in session_data)
        })
        
    except Exception as e:
        print(f"❌ Error in get_sessions_by_date: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to fetch sessions for date'
        }), 500

@app.route('/sessions/stats')
def get_session_stats():
    """Get overall session statistics"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        # Get today's stats
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        
        today_sessions = qr_sessions_collection.count_documents({
            "created_at": {"$gte": today_start, "$lt": today_end}
        })
        
        today_attendance = attendance_collection.count_documents({
            "session_date": today_start
        })
        
        # Get total stats
        total_sessions = qr_sessions_collection.count_documents({})
        total_attendance = attendance_collection.count_documents({})
        total_students = students_collection.count_documents({})
        
        # Get active sessions
        active_sessions = qr_sessions_collection.count_documents({
            "expires_at": {"$gt": datetime.now()},
            "is_active": True
        })
        
        return jsonify({
            'today': {
                'sessions': today_sessions,
                'attendance': today_attendance,
                'attendance_rate': f"{(today_attendance/total_students*100):.1f}%" if total_students > 0 else "0%"
            },
            'total': {
                'sessions': total_sessions,
                'attendance_records': total_attendance,
                'students': total_students
            },
            'active_sessions': active_sessions,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        print(f"❌ Error in get_session_stats: {e}")
        return jsonify({
            'error': str(e),
            'message': 'Failed to fetch session statistics'
        }), 500

@app.route('/qr/status')
def qr_status():
    """Get current QR status and timing information"""
    try:
        if not client:
            return jsonify({'error': 'Database not connected'}), 500
        
        current_time = datetime.now()
        
        # Get current active QR
        active_qr = qr_sessions_collection.find_one({
            "is_active": True,
            "expires_at": {"$gt": current_time}
        }, sort=[("created_at", -1)])
        
        if active_qr:
            time_remaining = (active_qr['expires_at'] - current_time).total_seconds()
            next_refresh = QR_AUTO_REFRESH_INTERVAL - (time_remaining % QR_AUTO_REFRESH_INTERVAL)
            
            return jsonify({
                'active': True,
                'qr_code': active_qr['qr_code'],
                'created_at': active_qr['created_at'].isoformat(),
                'expires_at': active_qr['expires_at'].isoformat(),
                'time_remaining': max(0, time_remaining),
                'next_refresh_in': max(0, next_refresh),
                'refresh_interval': QR_AUTO_REFRESH_INTERVAL,
                'auto_generation_active': qr_generation_thread and qr_generation_thread.is_alive(),
                'used_by_count': len(active_qr.get('used_by', []))
            })
        else:
            return jsonify({
                'active': False,
                'message': 'No active QR code',
                'auto_generation_active': qr_generation_thread and qr_generation_thread.is_alive(),
                'refresh_interval': QR_AUTO_REFRESH_INTERVAL
            })
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'message': 'Failed to get QR status'
        }), 500

# --- Faculty TOTP Setup and Verification ---

# Example: In production, store these in your faculty user collection in MongoDB
FACULTY_TOTP_SECRETS = {
    # Example: "faculty@kluniversity.edu": "BASE32SECRET"
    # Fill this with actual secrets per faculty user
}

def generate_qr_image_from_uri(uri):
    """Generate a QR code image (base64 PNG) from a provisioning URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{img_str}"

@app.route('/faculty/totp/setup', methods=['POST'])
def faculty_totp_setup():
    """
    Generate a TOTP secret and provisioning QR for a faculty user.
    Expects JSON: { "email": "faculty@kluniversity.edu" }
    """
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"error": "Email required"}), 400

    # Check if already exists
    faculty = faculty_collection.find_one({"email": email})
    if faculty and "totp_secret" in faculty:
        return jsonify({"error": "TOTP already set up for this user"}), 400

    # Generate and store secret
    secret = pyotp.random_base32()
    faculty_collection.update_one(
        {"email": email},
        {"$set": {"totp_secret": secret}},
        upsert=True
    )

    provisioning_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=email,
        issuer_name="KL University"
    )
    qr_image = generate_qr_image_from_uri(provisioning_uri)
    return jsonify({
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_image": qr_image
    })

@app.route('/faculty/totp/verify', methods=['POST'])
def faculty_totp_verify():
    """
    Verify a TOTP code for a faculty user.
    Expects JSON: { "email": "faculty@kluniversity.edu", "code": "123456" }
    """
    data = request.get_json()
    email = data.get("email")
    code = data.get("code")
    if not email or not code:
        return jsonify({"valid": False, "message": "Email and code required"}), 400

    faculty = faculty_collection.find_one({"email": email})
    if not faculty or "totp_secret" not in faculty:
        return jsonify({"valid": False, "message": "No TOTP secret found for this user"}), 404

    totp = pyotp.TOTP(faculty["totp_secret"])
    is_valid = totp.verify(code)
    return jsonify({"valid": is_valid})

# --- Debug / inspection endpoints (DO NOT expose publicly in production) ---
@app.route('/attendance/today')
def attendance_today():
    if not client:
        return jsonify({'error': 'Database not connected'}), 500
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    recs = list(attendance_collection.find({"session_date": start}).sort("marked_at", 1))
    return jsonify({
        'count': len(recs),
        'records': [{
            'student_id': r['student_id'],
            'student_name': r.get('student_name', ''),
            'marked_at': r['marked_at'].isoformat(),
            'qr_code': r.get('qr_code', ''),
            'qr_session_id': str(r.get('qr_session_id'))
        } for r in recs]
    })

@app.route('/attendance/session/<session_id>')
def attendance_for_session(session_id):
    if not client:
        return jsonify({'error': 'Database not connected'}), 500
    try:
        sid = ObjectId(session_id)
    except:
        return jsonify({'error': 'Invalid session id'}), 400
    recs = list(attendance_collection.find({"qr_session_id": sid}).sort("marked_at", 1))
    return jsonify({
        'session_id': session_id,
        'count': len(recs),
        'records': [{
            'student_id': r['student_id'],
            'student_name': r.get('student_name', ''),
            'marked_at': r['marked_at'].isoformat(),
            'qr_code': r.get('qr_code', '')
        } for r in recs]
    })
# --- End debug endpoints ---

if __name__ == '__main__':
    print("🚀 Starting Flask API with MongoDB Atlas...")
    
    # Initialize database
    if initialize_database():
        print("✅ Database initialization complete")
    else:
        print("❌ Database initialization failed")
    
    # Start auto QR generation
    start_auto_qr_generation()
    print(f"🔄 Auto QR generation started (every {QR_AUTO_REFRESH_INTERVAL} seconds)")
    
    print(f"🔗 MongoDB URI: mongodb+srv://megh:***@vicecluster.4wafcsu.mongodb.net/")
    print(f"📊 Database: {DATABASE_NAME}")
    print(f"🌐 Starting server on port {PORT}")
    
    # Run Flask API
    app.run(debug=False, port=PORT, host='0.0.0.0')

    # Start the auto QR generation in the background
    start_auto_qr_generation()