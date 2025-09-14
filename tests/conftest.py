# Ensure consistent SSL certificate verification using certifi's CA bundle.
import os, certifi  # noqa: F401

# Use certifi for OpenSSL-based libraries (requests, httpx, websockets, etc.).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
