import sqlite3

def init_db():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    
    # Bookings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            car_type TEXT,
            hourly_package TEXT,
            price TEXT,
            pickup_location TEXT,
            phone TEXT,
            status TEXT DEFAULT 'PENDING',
            driver_name TEXT
        )
    ''')
    
    # Driver wallets table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drivers (
            driver_id INTEGER PRIMARY KEY,
            driver_name TEXT,
            balance REAL DEFAULT 0.0
        )
    ''')
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()