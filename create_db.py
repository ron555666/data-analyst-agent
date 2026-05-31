import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).with_name("sales_data.db")


CUSTOMERS = [
    (1, "Ava Chen", "West", "retail"),
    (2, "Noah Smith", "East", "enterprise"),
    (3, "Mia Johnson", "South", "retail"),
    (4, "Liam Patel", "West", "small_business"),
    (5, "Sophia Garcia", "Midwest", "enterprise"),
]

PRODUCTS = [
    (1, "Analytics Starter", "Software", 99.0),
    (2, "Analytics Pro", "Software", 249.0),
    (3, "Dashboard Setup", "Service", 499.0),
    (4, "Data Cleaning Pack", "Service", 299.0),
    (5, "Team Training", "Education", 799.0),
]

ORDERS = [
    (1, "2026-01-05", 1, 1, 3, 0.00),
    (2, "2026-01-12", 2, 5, 1, 0.10),
    (3, "2026-01-22", 3, 2, 2, 0.05),
    (4, "2026-02-02", 4, 3, 1, 0.00),
    (5, "2026-02-18", 5, 2, 4, 0.15),
    (6, "2026-03-03", 1, 4, 2, 0.00),
    (7, "2026-03-16", 2, 2, 3, 0.10),
    (8, "2026-03-29", 3, 1, 6, 0.00),
    (9, "2026-04-08", 4, 5, 1, 0.05),
    (10, "2026-04-19", 5, 3, 2, 0.10),
    (11, "2026-05-04", 1, 2, 2, 0.00),
    (12, "2026-05-21", 2, 4, 5, 0.20),
]


def create_database(db_path: Path = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript(
        """
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS customers;

        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            region TEXT NOT NULL,
            segment TEXT NOT NULL
        );

        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL
        );

        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            order_date TEXT NOT NULL,
            customer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            discount REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        """
    )

    cur.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", CUSTOMERS)
    cur.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS)
    cur.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ORDERS)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    create_database()
    print(f"Created {DB_PATH}")
