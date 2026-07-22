#!/usr/bin/env bash
# Send a simulated SMS to the edge node and show the response
# Usage: ./scripts/send_sms.sh "What sessions are about edge computing?"

API_URL="${API_URL:-http://localhost:8000}"
PHONE="${PHONE:-+1234567890}"
MESSAGE="${1:-Hello, what is Summit Connect?}"

echo "Sending SMS from $PHONE: \"$MESSAGE\""
echo ""

RESPONSE=$(curl -s -X POST "$API_URL/sms/receive" \
  -H "Content-Type: application/json" \
  -d "{\"sender\": \"$PHONE\", \"receiver\": \"+1000000000\", \"content\": \"$MESSAGE\"}")

echo "Response: $RESPONSE"
echo ""

# Wait a moment for async processing, then check history
sleep 3

echo "Message history:"
curl -s "$API_URL/sms/history" | python3 -m json.tool 2>/dev/null || \
  curl -s "$API_URL/sms/history"
