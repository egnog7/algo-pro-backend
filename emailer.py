import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


def send_license_email(
    to_email: str,
    license_key: str,
    portal_url: str,
):
    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    email_from = os.getenv("EMAIL_FROM")

    if not sendgrid_api_key:
        raise RuntimeError("SENDGRID_API_KEY missing")

    if not email_from:
        raise RuntimeError("EMAIL_FROM missing")

    message = Mail(
        from_email=email_from,
        to_emails=to_email,
        subject="Your Algo Pro License",
        html_content=f"""
        <h2>Welcome to Algo Pro</h2>

        <p>Your license has been created successfully.</p>

        <p><strong>License Key:</strong><br>{license_key}</p>

        <p>
          Open your portal:<br>
          <a href="{portal_url}">{portal_url}</a>
        </p>

        <p>Algo Pro Support</p>
        """,
    )

    sg = SendGridAPIClient(sendgrid_api_key)
    response = sg.send(message)

    return response.status_code