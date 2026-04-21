import json
import boto3
from twilio.rest import Client
from twilio.request_validator import RequestValidator

def send_sms(from_number: str, to_number: str, body: str,
             subaccount_sid: str, subaccount_token: str) -> str:
    client = Client(subaccount_sid, subaccount_token)
    msg = client.messages.create(
        from_=from_number,
        to=to_number,
        body=body[:1600]
    )
    return msg.sid

def validate_signature(auth_token: str, signature: str, url: str, params: dict):
    validator = RequestValidator(auth_token)
    if not validator.validate(url, params, signature):
        raise ValueError("Invalid Twilio signature")
