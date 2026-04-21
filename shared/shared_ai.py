import json
import boto3
import requests

_api_key = None
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

def _get_api_key() -> str:
    global _api_key
    if _api_key is None:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        raw = client.get_secret_value(SecretId='rcm/gemini/api-key')['SecretString']
        _api_key = json.loads(raw)['api_key']
    return _api_key

def call_gemini(system_prompt: str, user_message: str, max_chars: int = 1600) -> str:
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {"maxOutputTokens": 800}
    }
    resp = requests.post(
        _GEMINI_URL,
        params={"key": _get_api_key()},
        json=payload,
        timeout=20
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return (text or '')[:max_chars]

def load_prompt_from_s3(practice_area: str, prompt_name: str = 'intake_v1') -> str:
    s3 = boto3.client('s3', region_name='us-east-2')
    key = f"prompts/{practice_area}/{prompt_name}.txt"
    obj = s3.get_object(Bucket='firmos-documents-006619321854', Key=key)
    return obj['Body'].read().decode('utf-8')
