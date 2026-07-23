"""Tiny HTTP server to serve the dashboard locally."""
import http.server
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
print(f"Dashboard at http://localhost:{port}?api=http://localhost:8000")
http.server.HTTPServer(("", port), http.server.SimpleHTTPRequestHandler).serve_forever()
