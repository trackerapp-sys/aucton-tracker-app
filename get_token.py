import requests

APP_ID = "1510045799998746"
APP_SECRET = "41b2613e2b6ee258d4a559b75c93fc2b"

# Get short-lived access token
url = f"https://graph.facebook.com/oauth/access_token?client_id={APP_ID}&client_secret={APP_SECRET}&grant_type=client_credentials"
response = requests.get(url)
print("Full Response:", response.json())

# Extract just the access token
if response.status_code == 200:
    data = response.json()
    access_token = data.get('access_token')
    print("\n=== YOUR FACEBOOK ACCESS TOKEN ===")
    print(access_token)
    print("=== COPY THIS TOKEN ===")
else:
    print("Error:", response.status_code, response.text)