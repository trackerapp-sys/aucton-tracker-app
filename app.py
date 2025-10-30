from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
import requests
import datetime
import threading
import time
from price_parser import Price
import re
import pytz
import os
from dotenv import load_dotenv
from datetime import datetime as dt

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Simple user class for authentication
class User(UserMixin):
    def __init__(self, id):
        self.id = id

# Mock user database (replace with real database in production)
users = {'admin': {'password': 'password123'}}  # Username: admin, Password: password123

@login_manager.user_loader
def load_user(user_id):
    return User(user_id) if user_id in users else None

class Auction:
    def __init__(self, post_id, start_time, end_time, starting_bid=0, timezone='Australia/Sydney'):
        self.post_id = post_id
        self.timezone = pytz.timezone(timezone)
        self.start_time = datetime.datetime.fromisoformat(start_time).astimezone(self.timezone)
        self.end_time = datetime.datetime.fromisoformat(end_time).astimezone(self.timezone)
        self.starting_bid = starting_bid
        self.current_bid = starting_bid
        self.current_bidder = None
        self.bid_history = []  # List of (bidder_id, bidder_name, amount, timestamp)
        self.active = False

    def is_active(self):
        now = datetime.datetime.now(self.timezone)
        if self.start_time <= now <= self.end_time:
            if not self.active:
                self.active = True
                self.announce_start()
            return True
        elif now > self.end_time and self.active:
            self.active = False
            self.select_winner()
        return False

    def parse_bid(self, comment_text, commenter_id, commenter_name):
        price = Price.fromstring(comment_text)
        if price and price.amount is not None and price.amount > self.current_bid:
            return float(price.amount)
        match = re.search(r'(\d+(?:\.\d{2})?)', comment_text)
        if match:
            amount = float(match.group(1))
            if amount > self.current_bid:
                return amount
        return None

    def add_bid(self, bidder_id, bidder_name, amount):
        self.current_bid = amount
        self.current_bidder = bidder_id
        timestamp = datetime.datetime.now(self.timezone)
        self.bid_history.append((bidder_id, bidder_name, amount, timestamp))
        self.notify_outbid(bidder_id, bidder_name)
        self.announce_new_bid(bidder_id, bidder_name, amount)

    def announce_start(self):
        self.post_to_post(f"Auction started! Starting bid: ${self.starting_bid}")

    def select_winner(self):
        if self.current_bidder:
            winner_msg = f"Auction ended! Winner: {self.current_bidder} with bid ${self.current_bid}!"
            self.post_to_post(winner_msg)
            self.notify_winner(self.current_bidder)

    def post_to_post(self, message):
        access_token = os.environ.get('FB_ACCESS_TOKEN')
        if not access_token:
            print("No Facebook access token configured")
            return
            
        url = f"https://graph.facebook.com/v19.0/{self.post_id}/comments"
        params = {'access_token': access_token, 'message': message}
        try:
            response = requests.post(url, params=params)
            if 'error' in response.json():
                print(f"Error posting comment: {response.json()['error']['message']}")
        except Exception as e:
            print(f"Error posting comment: {str(e)}")

    def announce_new_bid(self, bidder_id, bidder_name, amount):
        self.post_to_post(f"New bid: {bidder_name} bids ${amount}! Current high: ${amount}")

    def notify_outbid(self, bidder_id, bidder_name):
        print(f"Notification: {bidder_name} ({bidder_id}) outbid! New high: ${self.current_bid}")

    def notify_winner(self, winner_id):
        print(f"Congratulations! User {winner_id} won with ${self.current_bid}")

class FacebookAuctionManager:
    def __init__(self):
        self.access_token = os.environ.get('FB_ACCESS_TOKEN')
        self.auctions = {}  # post_id -> Auction
        self.monitoring = False
        self.timezone = pytz.timezone('Australia/Sydney')
        self.date_format = '%d/%m/%Y %H:%M'
        self.log_messages = []
        self.monitor_thread = None

    def start_monitoring(self):
        if self.monitoring:
            return
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.log_message("Monitoring started")

    def stop_monitoring(self):
        self.monitoring = False
        self.log_message("Monitoring stopped")

    def monitor_loop(self):
        while self.monitoring:
            for post_id, auction in list(self.auctions.items()):
                if auction.is_active():
                    self.check_comments(post_id, auction)
            time.sleep(30)  # Poll every 30 seconds

    def check_comments(self, post_id, auction):
        if not self.access_token:
            self.log_message("No access token configured")
            return
            
        url = f"https://graph.facebook.com/v19.0/{post_id}/comments"
        params = {'access_token': self.access_token, 'fields': 'message,from{id,name}'}
        try:
            response = requests.get(url, params=params)
            data = response.json()
            if 'error' in data:
                error_msg = data['error']['message']
                self.log_message(f"Error fetching comments for {post_id}: {error_msg}")
                return
            for comment in data.get('data', []):
                text = comment['message'].lower()
                bidder_id = comment['from']['id']
                bidder_name = comment['from']['name']
                amount = auction.parse_bid(text, bidder_id, bidder_name)
                if amount:
                    auction.add_bid(bidder_id, bidder_name, amount)
                    self.log_message(f"New bid on {post_id}: ${amount} by {bidder_name}")
        except Exception as e:
            error_str = str(e)
            self.log_message(f"Error checking comments for {post_id}: {error_str}")

    def add_auction(self, post_id, start_time, end_time, starting_bid, timezone='Australia/Sydney'):
        try:
            # Convert DD/MM/YYYY HH:MM to ISO format
            start_dt = dt.strptime(start_time, '%d/%m/%Y %H:%M')
            end_dt = dt.strptime(end_time, '%d/%m/%Y %H:%M')
            start_iso = start_dt.strftime('%Y-%m-%dT%H:%M')
            end_iso = end_dt.strftime('%Y-%m-%dT%H:%M')
            
            starting_bid = float(starting_bid or 0)
            self.auctions[post_id] = Auction(post_id, start_iso, end_iso, starting_bid, timezone)
            self.log_message(f"Auction added for post {post_id}")
            return True, "Auction added successfully"
        except ValueError as e:
            error_str = str(e)
            self.log_message(f"Error adding auction: {error_str}")
            return False, f"Invalid input: {error_str}"

    def log_message(self, message):
        timestamp = datetime.datetime.now(self.timezone).strftime(self.date_format)
        log_entry = f"[{timestamp}] {message}"
        self.log_messages.append(log_entry)
        # Keep only last 1000 log messages
        if len(self.log_messages) > 1000:
            self.log_messages.pop(0)
        print(log_entry)

    def get_auctions_data(self):
        auctions_data = []
        for post_id, auction in self.auctions.items():
            status = "Active" if auction.is_active() else "Ended"
            bidder = auction.current_bidder or "None"
            end_time = auction.end_time.strftime(self.date_format)
            auctions_data.append({
                'post_id': post_id,
                'current_bid': f"${auction.current_bid}",
                'bidder': bidder,
                'status': status,
                'end_time': end_time
            })
        return auctions_data

    def get_bid_history(self):
        history = {}
        for post_id, auction in self.auctions.items():
            history[post_id] = []
            for bidder_id, bidder_name, amount, timestamp in auction.bid_history:
                formatted_time = timestamp.strftime(self.date_format)
                history[post_id].append({
                    'time': formatted_time,
                    'bidder': bidder_name,
                    'amount': f"${amount}"
                })
        return history

# Global manager instance
manager = FacebookAuctionManager()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username in users and users[username]['password'] == password:
            user = User(username)
            login_user(user)
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/auctions', methods=['GET'])
@login_required
def get_auctions():
    return jsonify(manager.get_auctions_data())

@app.route('/api/auctions', methods=['POST'])
@login_required
def add_auction():
    data = request.json
    success, message = manager.add_auction(
        data['post_id'],
        data['start_time'],
        data['end_time'],
        data['starting_bid'],
        data.get('timezone', 'Australia/Sydney')
    )
    return jsonify({'success': success, 'message': message})

@app.route('/api/auctions/<post_id>', methods=['DELETE'])
@login_required
def delete_auction(post_id):
    if post_id in manager.auctions:
        del manager.auctions[post_id]
        manager.log_message(f"Auction {post_id} deleted")
        return jsonify({'success': True, 'message': 'Auction deleted'})
    return jsonify({'success': False, 'message': 'Auction not found'})

@app.route('/api/monitoring', methods=['POST'])
@login_required
def toggle_monitoring():
    action = request.json.get('action')
    if action == 'start':
        manager.start_monitoring()
        return jsonify({'success': True, 'message': 'Monitoring started'})
    elif action == 'stop':
        manager.stop_monitoring()
        return jsonify({'success': True, 'message': 'Monitoring stopped'})
    return jsonify({'success': False, 'message': 'Invalid action'})

@app.route('/api/monitoring/status')
@login_required
def monitoring_status():
    return jsonify({'monitoring': manager.monitoring})

@app.route('/api/logs')
@login_required
def get_logs():
    return jsonify({'logs': manager.log_messages[-100:]})  # Last 100 log entries

@app.route('/api/analytics')
@login_required
def get_analytics():
    return jsonify(manager.get_bid_history())

@app.route('/api/export')
@login_required
def export_bids():
    if not manager.auctions:
        return jsonify({'success': False, 'message': 'No auctions to export'})
    
    timestamp = datetime.datetime.now(manager.timezone).strftime("%Y%m%d_%H%M%S")
    filename = f"auction_bids_{timestamp}.txt"
    
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            for post_id, auction in manager.auctions.items():
                f.write(f"Auction {post_id}:\n")
                for bidder_id, bidder_name, amount, timestamp in auction.bid_history:
                    formatted_time = timestamp.strftime(manager.date_format)
                    f.write(f"  {formatted_time}: {bidder_name} bid ${amount}\n")
        
        manager.log_message(f"Bids exported to {filename}")
        return send_file(filename, as_attachment=True)
    except Exception as e:
        error_str = str(e)
        manager.log_message(f"Error exporting bids: {error_str}")
        return jsonify({'success': False, 'message': f'Export failed: {error_str}'})

@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.json
    manager.timezone = pytz.timezone(data['timezone'])
    manager.date_format = data['date_format']
    
    # Update all existing auctions
    for auction in manager.auctions.values():
        auction.timezone = manager.timezone
        auction.start_time = auction.start_time.astimezone(manager.timezone)
        auction.end_time = auction.end_time.astimezone(manager.timezone)
        auction.bid_history = [
            (bidder_id, bidder_name, amount, timestamp.astimezone(manager.timezone)) 
            for bidder_id, bidder_name, amount, timestamp in auction.bid_history
        ]
    
    manager.log_message(f"Settings updated: Time zone {data['timezone']}, format {data['date_format']}")
    return jsonify({'success': True, 'message': 'Settings updated'})

@app.route('/test')
def test():
    return "App is working! If you see this, Flask is running correctly."

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'message': 'Server is running'})

# Policy Routes for Meta Review
@app.route('/privacy-policy')
def privacy_policy():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Privacy Policy - Facebook Auction Tracker</title>
        <style>
            body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; padding: 20px; max-width: 800px; margin: 0 auto; }
            h1 { color: #333; border-bottom: 2px solid #f0f0f0; padding-bottom: 10px; }
            h2 { color: #555; margin-top: 30px; }
            .last-updated { color: #666; font-style: italic; }
        </style>
    </head>
    <body>
        <h1>Privacy Policy</h1>
        <p class="last-updated">Last updated: 2024</p>

        <h2>1. Information We Collect</h2>
        <p>Our Facebook Auction Tracker application collects the following information when you grant permission:</p>
        <ul>
            <li>Basic profile information (name, email, user ID)</li>
            <li>Posts and comments for auction monitoring</li>
            <li>Bid information from auction comments</li>
        </ul>

        <h2>2. How We Use Your Information</h2>
        <p>We use the collected information solely for:</p>
        <ul>
            <li>Monitoring auction posts and comments</li>
            <li>Tracking bids and bidder information</li>
            <li>Managing auction timelines and winners</li>
            <li>Posting auction updates and notifications</li>
        </ul>

        <h2>3. Data Storage and Security</h2>
        <p>We do not store any of your Facebook data on our servers. All data is:</p>
        <ul>
            <li>Processed locally on your device</li>
            <li>Only stored in your local browser session</li>
            <li>Never transmitted to any external servers except Facebook's API</li>
            <li>Deleted when you close the application</li>
        </ul>

        <h2>4. Data Sharing</h2>
        <p>We do not share, sell, or distribute your personal information to any third parties. Your data remains exclusively on your local device.</p>

        <h2>5. Your Rights</h2>
        <p>You have the right to:</p>
        <ul>
            <li>Access the data we process</li>
            <li>Delete all locally stored data by closing the application</li>
            <li>Revoke Facebook permissions at any time through your Facebook settings</li>
        </ul>

        <h2>6. Facebook API Compliance</h2>
        <p>Our application complies with Facebook's Platform Policies and only accesses data you explicitly permit through the Facebook login process.</p>

        <h2>7. Contact Information</h2>
        <p>If you have any questions about this Privacy Policy, please contact us at: your-email@domain.com</p>

        <h2>8. Changes to This Policy</h2>
        <p>We may update this privacy policy from time to time. We will notify you of any changes by posting the new policy on this page.</p>
    </body>
    </html>
    """

@app.route('/terms-of-service')
def terms_of_service():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Terms of Service - Facebook Auction Tracker</title>
        <style>
            body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; padding: 20px; max-width: 800px; margin: 0 auto; }
            h1 { color: #333; border-bottom: 2px solid #f0f0f0; padding-bottom: 10px; }
            h2 { color: #555; margin-top: 30px; }
            .last-updated { color: #666; font-style: italic; }
        </style>
    </head>
    <body>
        <h1>Terms of Service</h1>
        <p class="last-updated">Last updated: 2024</p>

        <h2>1. Acceptance of Terms</h2>
        <p>By using the Facebook Auction Tracker application, you agree to be bound by these Terms of Service.</p>

        <h2>2. Description of Service</h2>
        <p>Our application provides automated auction monitoring and management for Facebook posts. The service tracks bids, manages auction timelines, and posts updates through Facebook comments.</p>

        <h2>3. User Responsibilities</h2>
        <p>You agree to:</p>
        <ul>
            <li>Use the application in compliance with Facebook's Platform Policies</li>
            <li>Not use the application for any illegal or unauthorized purpose</li>
            <li>Maintain the security of your Facebook access tokens</li>
            <li>Only monitor auctions you have permission to manage</li>
        </ul>

        <h2>4. Data Handling</h2>
        <p>All data processing occurs locally on your device. We do not store, transmit, or process your Facebook data on any external servers.</p>

        <h2>5. Intellectual Property</h2>
        <p>The application and its original content are owned by us. Your Facebook data remains your property.</p>

        <h2>6. Termination</h2>
        <p>We may terminate or suspend access to our application immediately if you violate these Terms.</p>

        <h2>7. Limitation of Liability</h2>
        <p>We are not liable for any damages resulting from your use of the application.</p>

        <h2>8. Changes to Terms</h2>
        <p>We reserve the right to modify these terms at any time.</p>

        <h2>9. Contact</h2>
        <p>Questions about these Terms? Contact: your-email@domain.com</p>
    </body>
    </html>
    """

@app.route('/data-deletion')
def data_deletion():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Data Deletion Instructions - Facebook Auction Tracker</title>
        <style>
            body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; padding: 20px; max-width: 800px; margin: 0 auto; }
            h1 { color: #333; border-bottom: 2px solid #f0f0f0; padding-bottom: 10px; }
            h2 { color: #555; margin-top: 30px; }
            .last-updated { color: #666; font-style: italic; }
        </style>
    </head>
    <body>
        <h1>Data Deletion Instructions</h1>
        <p class="last-updated">Last updated: 2024</p>

        <h2>How to Delete Your Data</h2>
        <p>Since our application does not store any of your personal data on external servers, data deletion is simple:</p>

        <h3>Option 1: Close the Application</h3>
        <p>All your data is stored locally in your browser session. Simply close the application and all data will be automatically deleted.</p>

        <h3>Option 2: Clear Browser Data</h3>
        <p>If you want to ensure complete deletion:</p>
        <ul>
            <li>Open your browser settings</li>
            <li>Navigate to "Privacy and Security"</li>
            <li>Click "Clear browsing data"</li>
            <li>Select "Cached images and files" and "Site data"</li>
            <li>Click "Clear data"</li>
        </ul>

        <h3>Option 3: Revoke Facebook Permissions</h3>
        <p>To remove our application's access to your Facebook data:</p>
        <ul>
            <li>Go to your Facebook Settings</li>
            <li>Click "Apps and Websites"</li>
            <li>Find "Facebook Auction Tracker" in your active apps</li>
            <li>Click "Remove" to revoke all permissions</li>
        </ul>

        <h2>Contact</h2>
        <p>If you need assistance with data deletion, contact us at: your-email@domain.com</p>

        <p><strong>Note:</strong> We never store your personal data on our servers, so no server-side data deletion is required.</p>
    </body>
    </html>
    """

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)