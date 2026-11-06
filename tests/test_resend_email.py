
import os
import resend
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("RESEND_API_KEY")

if not api_key:
    raise ValueError("RESEND_API_KEY missing in .env")

resend.api_key = api_key

response = resend.Emails.send({
    "from": "JAIWER <onboarding@resend.dev>",
    "to": ["adityaraj.dev08@gmail.com"],
    "subject": "JAIWER Email Test",
    "html": """
        <h2>JAIWER Email Testing Successful!</h2>
        <p>Your Resend integration is working.</p>
    """
})

print("Email API response:", response)
