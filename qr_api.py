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

# Load environment variables
load_dotenv()

app = Flask(__name__)

# CORS Configuration for production deployment
CORS(app, 
     origins=["*"],
     methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
     allow_headers=["Content-Type", "Authorization"],
     supports_credentials=True
)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# Configuration from environment variables
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb+srv://megh:marco@vicecluster.4wafcsu.mongodb.net/?retryWrites=true&w=majority&appName=vicecluster')
DATABASE_NAME = os.getenv('DATABASE_NAME', 'kl_university_attendance')
STUDENTS_COLLECTION = os.getenv('STUDENTS_COLLECTION', 'students')
ATTENDANCE_COLLECTION = os.getenv('ATTENDANCE_COLLECTION', 'attendance_records')
QR_SESSIONS_COLLECTION = os.getenv('QR_SESSIONS_COLLECTION', 'qr_sessions')
PORT = int(os.getenv('PORT', 5000))

# Initialize MongoDB client
try:
    client = MongoClient(MONGODB_URI)
    db = client[DATABASE_NAME]
    students_collection = db[STUDENTS_COLLECTION]
    attendance_collection = db[ATTENDANCE_COLLECTION]
    qr_sessions_collection = db[QR_SESSIONS_COLLECTION]
    
    # Test connection
    client.admin.command('ping')
    print("✅ Successfully connected to MongoDB Atlas!")
    
except Exception as e:
    print(f"❌ Failed to connect to MongoDB: {e}")
    client = None

# Configuration
QR_VALIDITY_SECONDS = 30

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
    """Remove expired QR codes from MongoDB"""
    try:
        if not client:
            return 0
            
        current_time = datetime.now()
        result = qr_sessions_collection.delete_many({
            "expires_at": {"$lt": current_time}
        })
        if result.deleted_count > 0:
            print(f"🧹 Cleaned up {result.deleted_count} expired QR codes")
        return result.deleted_count
    except Exception as e:
        print(f"❌ Error cleaning up QR codes: {e}")
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
    """Generate and return a new QR code"""
    try:
        if not client:
            return jsonify({"error": "Database not connected"}), 500
            
        cleanup_expired_qr_codes()
        
        # Generate QR data
        qr_data = generate_random_data()
        qr_image = generate_qr_image(qr_data)
        
        if qr_image is None:
            return jsonify({"error": "Failed to generate QR code"}), 500
        
        # Store QR session in MongoDB
        current_time = datetime.now()
        expires_at = current_time + timedelta(seconds=QR_VALIDITY_SECONDS)
        
        qr_session = {
            "qr_code": qr_data,
            "created_at": current_time,
            "expires_at": expires_at,
            "is_active": True,
            "used_by": []
        }
        
        result = qr_sessions_collection.insert_one(qr_session)
        
        print(f"📱 Generated QR code: {qr_data} (expires at {expires_at})")
        
        return jsonify({
            "data": qr_data,
            "image": qr_image,
            "timestamp": current_time.isoformat(),
            "expires_at": expires_at.isoformat(),
            "expires_in": QR_VALIDITY_SECONDS,
            "session_id": str(result.inserted_id)
        })
        
    except Exception as e:
        print(f"❌ Error in get_qr: {e}")
        return jsonify({"error": str(e)}), 500

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
        
        # Log the raw request data for debugging
        print(f"🔍 Raw request data: {request.data}")
        print(f"🔍 Request headers: {dict(request.headers)}")
        print(f"🔍 Content-Type: {request.content_type}")
        
        # Better JSON parsing with error handling
        try:
            data = request.get_json(force=True)  # Add force=True to handle edge cases
            print(f"🔍 Parsed JSON data: {data}")
            
            if not data:
                return jsonify({
                    'valid': False,
                    'message': 'No JSON data provided'
                }), 400
        except Exception as json_error:
            print(f"❌ JSON parsing error: {json_error}")
            return jsonify({
                'valid': False,
                'message': f'Invalid JSON format: {str(json_error)}'
            }), 400
            
        qr_code = data.get('qr_code', '').strip()
        student_id = data.get('student_id', '').strip()
        student_name = data.get('student_name', '').strip()
        
        print(f"🔍 Validation request: QR='{qr_code}', Student='{student_id}', Name='{student_name}'")
        
        # Validate input with more specific error messages
        if not qr_code:
            print("❌ QR code is missing or empty")
            return jsonify({
                'valid': False,
                'message': 'QR code is required'
            }), 400
            
        if not student_id:
            print("❌ Student ID is missing or empty")
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
            qr_session = qr_sessions_collection.find_one({
                "qr_code": qr_code,
                "expires_at": {"$gt": current_time},
                "is_active": True
            })
        except Exception as qr_error:
            print(f"❌ Database error finding QR session: {qr_error}")
            return jsonify({
                'valid': False,
                'message': 'Database error while validating QR code'
            }), 500
        
        if not qr_session:
            # Check if QR exists but expired
            expired_qr = qr_sessions_collection.find_one({"qr_code": qr_code})
            if expired_qr:
                return jsonify({
                    'valid': False,
                    'message': 'QR code has expired. Please scan a new one.'
                }), 400
            else:
                return jsonify({
                    'valid': False,
                    'message': 'Invalid QR code. Please scan a valid QR code.'
                }), 400
        
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

@app.route('/debug/database')
def debug_database():
    """Debug endpoint to check database status"""
    try:
        if not client:
            return jsonify({
                'database_connected': False,
                'error': 'MongoDB client not initialized'
            }), 500
            
        # Test database connection
        client.admin.command('ping')
        
        return jsonify({
            'database_connected': True,
            'database_name': DATABASE_NAME,
            'mongodb_uri': 'mongodb+srv://megh:***@vicecluster.4wafcsu.mongodb.net/',
            'collections': {
                'students': students_collection.count_documents({}),
                'attendance': attendance_collection.count_documents({}),
                'qr_sessions': qr_sessions_collection.count_documents({})
            },
            'active_qr_sessions': qr_sessions_collection.count_documents({
                "expires_at": {"$gt": datetime.now()},
                "is_active": True
            })
        })
    except Exception as e:
        print(f"❌ Database debug error: {e}")
        return jsonify({
            'database_connected': False,
            'error': str(e)
        }), 500

@app.route('/debug/network')
def debug_network():
    """Network debugging endpoint"""
    import socket
    
    # Get actual server network info
    hostname = socket.gethostname()
    try:
        # Get local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "Unable to determine"
    
    return jsonify({
        'message': 'Network connection working!',
        'server_hostname': hostname,
        'server_local_ip': local_ip,
        'client_ip': request.remote_addr,
        'user_agent': request.headers.get('User-Agent', ''),
        'timestamp': datetime.now().isoformat(),
        'api_accessible': True,
        'cors_enabled': True,
        'mongodb_connected': client is not None,
        'environment': os.getenv('RENDER', 'local')
    })

if __name__ == '__main__':
    print("🚀 Starting Flask API with MongoDB Atlas...")
    
    # Initialize database
    if initialize_database():
        print("✅ Database initialization complete")
    else:
        print("❌ Database initialization failed")
    
    print(f"🔗 MongoDB URI: mongodb+srv://megh:***@vicecluster.4wafcsu.mongodb.net/")
    print(f"📊 Database: {DATABASE_NAME}")
    print(f"🌐 Starting server on port {PORT}")
    
    # Run Flask API
    app.run(debug=False, port=PORT, host='0.0.0.0')