import streamlit as st
import requests

st.title("Alibaba Dropshipping Dashboard")

try:
    response = requests.get("http://fastapi:8000/")
    st.write(response.json())
except requests.exceptions.ConnectionError as e:
    st.error(f"Failed to connect to the FastAPI backend: {e}")
