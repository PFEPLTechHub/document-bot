from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime, timedelta
import psycopg

app = Flask(__name__, static_folder='webapp')
CORS(app)  # Enable CORS for all routes

# Database configuration
DATABASE_URL = "postgresql://postgres:root@localhost:5432/telegram_bot_db"




def get_db_connection():
    return psycopg.connect(DATABASE_URL)

def init_database():
    """Initialize database tables"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Drop existing history table if it exists
            cur.execute("DROP TABLE IF EXISTS history CASCADE")
            
            # Create history table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    file_id BIGINT NOT NULL,
                    session_id UUID NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (file_id) REFERENCES files(id),
                    FOREIGN KEY (session_id) REFERENCES upload_sessions(session_id)
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"Error initializing database: {e}")
    finally:
        conn.close()

# Initialize database on startup
init_database()

@app.route('/')
def index():
    return send_from_directory('webapp', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('webapp', path)

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    # Check file count in temp directory
    temp_dir = "temp_uploads"
    os.makedirs(temp_dir, exist_ok=True)
    current_files = len([f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))])
    
    if current_files >= 4:
        return jsonify({'error': 'Maximum file limit reached (4/4)'}), 400
    
    # Save file to temp directory
    file_path = os.path.join(temp_dir, file.filename)
    file.save(file_path)
    
    return jsonify({'message': 'File uploaded successfully'}), 200

@app.route('/api/history', methods=['GET'])
def get_history():
    conn = None
    try:
        # Get user info from request headers
        user_id = request.headers.get('X-User-ID')
        
        print(f"\n=== Starting History Request ===")
        print(f"User ID: {user_id}")
        
        if not user_id:
            print("Error: Missing user information")
            return jsonify({'error': 'User information not provided'}), 400

        conn = get_db_connection()
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # First, get user's role from database
            cur.execute("""
                SELECT role, first_name, manager_id 
                FROM users 
                WHERE telegram_id = %s
            """, [user_id])
            user_info = cur.fetchone()
            
            if not user_info:
                print("Error: User not found in database")
                return jsonify({'error': 'User not found'}), 404
                
            user_role = user_info['role']
            print(f"User Role from DB: {user_role}")
            print(f"User Info: {user_info}")
            
            # Base query
            query = """
                SELECT 
                    f.id,
                    f.original_name,
                    f.file_size,
                    f.validation_status,
                    f.validation_errors,
                    f.created_at as session_date,
                    u.first_name as employee_name,
                    u.telegram_id as user_id,
                    u.role as user_role,
                    u.manager_id
                FROM files f
                JOIN upload_sessions s ON f.session_id = s.session_id
                JOIN users u ON s.user_id = u.telegram_id
            """
            
            # Add role-based filtering
            if user_role == 0:  # Employee
                print("\n=== Employee View ===")
                query += " WHERE u.telegram_id = %s"
                params = [user_id]
                print(f"Query: {query}")
                print(f"Params: {params}")
                
            elif user_role == 2:  # Manager
                print("\n=== Manager View ===")
                # Get all employees under this manager
                cur.execute("""
                    SELECT telegram_id, first_name, role, manager_id 
                    FROM users 
                    WHERE manager_id = %s
                """, [user_id])
                employees = cur.fetchall()
                print(f"\nEmployees under manager:")
                for emp in employees:
                    print(f"- {emp['first_name']} (ID: {emp['telegram_id']}, Role: {emp['role']}, Manager: {emp['manager_id']})")
                
                employee_ids = [row['telegram_id'] for row in employees]
                print(f"\nEmployee IDs: {employee_ids}")
                
                # Add manager's own ID to the list
                employee_ids.append(int(user_id))
                print(f"All IDs to query (including manager): {employee_ids}")
                
                # Create the WHERE clause with all relevant user IDs
                placeholders = ','.join(['%s'] * len(employee_ids))
                query += f" WHERE u.telegram_id IN ({placeholders})"
                params = employee_ids
                print(f"\nFinal Query: {query}")
                print(f"Query Params: {params}")
                
            else:  # Admin or other roles
                print("\n=== Other Role View ===")
                params = []
            
            query += " ORDER BY f.created_at DESC"
            
            print("\n=== Executing Main Query ===")
            cur.execute(query, params)
            history = cur.fetchall()
            print(f"Found {len(history)} history records")
            

            
            # Debug history records
            print("\n=== History Records ===")
            for record in history[:5]:  # Show first 5 records
                print(f"- {record['employee_name']} (ID: {record['user_id']}, Role: {record['user_role']}, Manager: {record['manager_id']})")
            
            # Get unique employees for filter dropdown
            if user_role == 2:  # Only get employees for managers
                print("\n=== Getting Employee List for Dropdown ===")
                cur.execute("""
                    SELECT DISTINCT u.first_name, u.telegram_id, u.role, u.manager_id
                    FROM users u
                    WHERE u.manager_id = %s AND u.role = 0
                    ORDER BY u.first_name
                """, [user_id])
                employees = cur.fetchall()
                print(f"Found {len(employees)} employees for dropdown")
                for emp in employees:
                    print(f"- {emp['first_name']} (ID: {emp['telegram_id']}, Role: {emp['role']}, Manager: {emp['manager_id']})")
            else:
                employees = []
            
            return jsonify({
                'history': history,
                'employees': employees
            }), 200
            
    except Exception as e:
        print(f"\n=== Error Occurred ===")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()
        print("\n=== Request Complete ===\n")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True) 