from fastapi import FastAPI
from celery import Celery
import os
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values
import numpy as np
from crawl4ai import AsyncWebCrawler  # Use AsyncWebCrawler instead of WebScraper
from pydantic import BaseModel, Field
import asyncio
import httpx
from typing import Optional, List
from python_unsplash import Unsplash
import json
import requests
from datetime import datetime

load_dotenv()

# Alibaba API Configuration
ALIBABA_APP_KEY = os.getenv("ALIBABA_APP_KEY")
ALIBABA_APP_SECRET = os.getenv("ALIBABA_APP_SECRET")
if not ALIBABA_APP_KEY or not ALIBABA_APP_SECRET:
    raise Exception("Alibaba API credentials are required")

def get_alibaba_access_token():
    """Get Alibaba API access token using OAuth 2.0"""
    url = f"https://gw.api.alibaba.com/openapi/param2/1/system.oauth2/getToken/{ALIBABA_APP_KEY}"
    payload = {
        "grant_type": "client_credentials",
        "client_id": ALIBABA_APP_KEY,
        "client_secret": ALIBABA_APP_SECRET
    }
    response = requests.post(url, data=payload)
    if response.status_code != 200:
        raise Exception(f"Failed to get Alibaba access token: {response.text}")
    return response.json().get("access_token")

# Initialize Alibaba access token
ALIBABA_ACCESS_TOKEN = get_alibaba_access_token()

class OrderStatus(BaseModel):
    order_id: int
    customer_id: int
    product_id: int
    quantity: int
    status: str
    alibaba_order_id: Optional[str]
    tracking_info: Optional[dict]

async def place_alibaba_order(product_id: str, quantity: int) -> dict:
    """Place an order on Alibaba"""
    url = f"https://gw.api.alibaba.com/openapi/param2/1/aliexpress.trade/buyers.createOrder/{ALIBABA_APP_KEY}"
    headers = {"Authorization": f"Bearer {ALIBABA_ACCESS_TOKEN}"}
    payload = {
        "product_id": product_id,
        "quantity": quantity,
        "logistics_type": "express",
    }
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        raise Exception(f"Failed to place Alibaba order: {response.text}")
    return response.json()

async def check_order_status(alibaba_order_id: str) -> dict:
    """Check shipping status of an Alibaba order"""
    url = f"https://gw.api.alibaba.com/openapi/param2/1/aliexpress.trade/buyers.getLogisticsInfo/{ALIBABA_APP_KEY}"
    headers = {"Authorization": f"Bearer {ALIBABA_ACCESS_TOKEN}"}
    params = {"order_id": alibaba_order_id}
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        raise Exception(f"Failed to get tracking info: {response.text}")
    return response.json()

@celery_app.task
async def process_pending_orders():
    """Process all pending orders"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Get pending orders
        cur.execute("""
            SELECT id, customer_id, product_id, quantity 
            FROM orders 
            WHERE status = 'pending'
        """)
        orders = cur.fetchall()
        
        for order_id, customer_id, product_id, quantity in orders:
            try:
                # Place order on Alibaba
                alibaba_response = await place_alibaba_order(product_id, quantity)
                alibaba_order_id = alibaba_response.get("order_id")
                
                # Update order status
                cur.execute("""
                    UPDATE orders 
                    SET status = 'processing', 
                        alibaba_order_id = %s,
                        updated_at = %s
                    WHERE id = %s
                """, (alibaba_order_id, datetime.now(), order_id))
                conn.commit()
                
            except Exception as e:
                print(f"Error processing order {order_id}: {str(e)}")
                continue
    
    finally:
        cur.close()
        conn.close()

@celery_app.task
async def update_order_tracking():
    """Update tracking information for processing orders"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Get processing orders
        cur.execute("""
            SELECT id, alibaba_order_id 
            FROM orders 
            WHERE status = 'processing'
        """)
        orders = cur.fetchall()
        
        for order_id, alibaba_order_id in orders:
            try:
                # Get tracking info
                tracking_info = await check_order_status(alibaba_order_id)
                
                # Update order tracking info
                cur.execute("""
                    UPDATE orders 
                    SET tracking_info = %s,
                        updated_at = %s
                    WHERE id = %s
                """, (json.dumps(tracking_info), datetime.now(), order_id))
                conn.commit()
                
            except Exception as e:
                print(f"Error updating tracking for order {order_id}: {str(e)}")
                continue
    
    finally:
        cur.close()
        conn.close()

@app.post("/orders/process")
async def trigger_order_processing():
    """Manually trigger processing of pending orders"""
    task = process_pending_orders.delay()
    return {"message": "Order processing started", "task_id": str(task.id)}

@app.get("/orders/{order_id}")
async def get_order_status(order_id: int):
    """Get status of a specific order"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT id, customer_id, product_id, quantity, status, 
                   alibaba_order_id, tracking_info
            FROM orders 
            WHERE id = %s
        """, (order_id,))
        
        order = cur.fetchone()
        if not order:
            return {"error": "Order not found"}
            
        return OrderStatus(
            order_id=order[0],
            customer_id=order[1],
            product_id=order[2],
            quantity=order[3],
            status=order[4],
            alibaba_order_id=order[5],
            tracking_info=order[6]
        )
    
    finally:
        cur.close()
        conn.close()


# Initialize Unsplash client
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
if not UNSPLASH_ACCESS_KEY:
    raise Exception("UNSPLASH_ACCESS_KEY environment variable is required")

unsplash = Unsplash(UNSPLASH_ACCESS_KEY)
app = FastAPI()

# DeepSeek API configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Initialize Celery
celery_app = Celery(
    "worker",
    broker=os.getenv("REDIS_URL", "redis://redis:6380/0"),
    backend=os.getenv("REDIS_URL", "redis://redis:6380/0"),
)

# Initialize PostgreSQL connection
def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("POSTGRES_DB", "dropshipping"),
        user=os.getenv("POSTGRES_USER", "admin"),
        password=os.getenv("POSTGRES_PASSWORD", "password"),
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=os.getenv("POSTGRES_PORT", "5433"),
    )

# Define a Pydantic model for structured responses
class ImageInfo(BaseModel):
    url: str = Field(description="The URL of the image")
    alt_text: str = Field(description="Alternative text for the image")
    credit: dict = Field(description="Credit information for the image")

class ProductInfo(BaseModel):
    name: str = Field(description="The name of the product")
    base_price: float = Field(description="The base price of the product")
    markup_percentage: float = Field(default=30.0, description="Markup percentage for the product")
    selling_price: float = Field(description="The selling price of the product")
    description: str = Field(description="A brief description of the product")
    category: str = Field(description="The category of the product")
    images: List[ImageInfo] = Field(default_factory=list, description="List of product images")

async def fetch_product_images(product_name: str, category: str, count: int = 3) -> List[ImageInfo]:
    """Fetch relevant images from Unsplash based on product details"""
    search_query = f"{product_name} {category}"
    photos = unsplash.search.photos(search_query, per_page=count)
    
    images = []
    for photo in photos['results']:
        images.append(ImageInfo(
            url=photo['urls']['regular'],
            alt_text=photo['alt_description'] or product_name,
            credit={
                'name': photo['user']['name'],
                'username': photo['user']['username'],
                'link': photo['user']['links']['html']
            }
        ))
    return images

async def extract_product_info_from_text(text: str) -> ProductInfo:
    """Extract product information using DeepSeek API"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "messages": [
                    {"role": "system", "content": "Extract product information from the provided text."},
                    {"role": "user", "content": text}
                ],
                "response_format": {"type": "json_object"}
            },
            timeout=30.0
        )
        
        if response.status_code != 200:
            raise Exception(f"DeepSeek API error: {response.text}")
            
        result = response.json()
        content = result['choices'][0]['message']['content']
        return ProductInfo.parse_raw(content)

@app.get("/")
def read_root():
    return {"message": "Alibaba Dropshipping Backend"}

@celery_app.task
def scrape_products(url):
    # Scrape product data using Crawl4AI
    async def run_scraper():
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
            return result.markdown
    return asyncio.run(run_scraper())

@celery_app.task
def extract_product_info(text):
    # Use DeepSeek API to extract structured product information
    result = asyncio.run(extract_product_info_from_text(text))
    return result.dict()

@celery_app.task
async def store_product_info(product_info):
    # Calculate selling price
    selling_price = product_info["base_price"] * (1 + product_info["markup_percentage"] / 100)
    
    # Fetch images for the product
    images = await fetch_product_images(product_info["name"], product_info["category"])
    
    # Store product information in PostgreSQL
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Begin transaction
        cur.execute("BEGIN")
        
        # Insert product with price range
        cur.execute(
            """
            INSERT INTO products 
            (name, base_price, markup_percentage, selling_price, price_range, description, category, images, status)
            VALUES (%s, %s, %s, %s, numrange(%s, %s), %s, %s, %s, 'active')
            RETURNING id
            """,
            (
                product_info["name"],
                product_info["base_price"],
                product_info["markup_percentage"],
                selling_price,
                selling_price * 0.9,  # Lower bound: 10% below selling price
                selling_price * 1.1,  # Upper bound: 10% above selling price
                product_info["description"],
                product_info["category"],
                json.dumps([img.dict() for img in images])
            ),
        )
        product_id = cur.fetchone()[0]
        
        # Update any pending orders that match this product's criteria
        cur.execute(
            """
            UPDATE orders o
            SET status = 'processing'
            FROM order_items oi
            WHERE o.id = oi.order_id
            AND o.status = 'pending'
            AND oi.product_id IS NULL
            AND EXISTS (
                SELECT 1
                FROM products p
                WHERE p.id = %s
                AND p.category = oi.category
                AND p.price_range @> oi.target_price
            )
            RETURNING o.id
            """,
            (product_id,)
        )
        
        # Link the product to matching order items
        cur.execute(
            """
            UPDATE order_items oi
            SET product_id = %s
            WHERE product_id IS NULL
            AND EXISTS (
                SELECT 1
                FROM products p
                WHERE p.id = %s
                AND p.category = oi.category
                AND p.price_range @> oi.target_price
            )
            """,
            (product_id, product_id)
        )
        
        # Commit transaction
        cur.execute("COMMIT")
        return str(product_id)
        
    except Exception as e:
        cur.execute("ROLLBACK")
        raise e
    finally:
        cur.close()
        conn.close()

@app.post("/products/create")
async def create_product(url: str):
    """Create a new product listing from a URL and sync with pending orders"""
    # Start the scraping task
    scrape_task = scrape_products.delay(url)
    scraped_data = scrape_task.get()
    
    # Extract product information
    product_info = await extract_product_info_from_text(scraped_data)
    
    # Store product with images and sync with orders
    product_id = await store_product_info(product_info.dict())
    
    # Trigger order processing for any newly matched orders
    process_pending_orders.delay()
    
    return {
        "message": "Product created successfully and synced with pending orders",
        "product_id": product_id
    }

@app.get("/products/pending-matches")
async def get_pending_product_matches():
    """Get statistics about pending orders waiting for product matches"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT 
                oi.category,
                COUNT(*) as pending_count,
                AVG(oi.target_price) as avg_target_price
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.status = 'pending'
            AND oi.product_id IS NULL
            GROUP BY oi.category
            ORDER BY pending_count DESC
        """)
        
        results = []
        for row in cur.fetchall():
            results.append({
                "category": row[0],
                "pending_count": row[1],
                "avg_target_price": float(row[2])
            })
            
        return results
    finally:
        cur.close()
        conn.close()

# Export the Celery app for worker discovery
celery = celery_app
