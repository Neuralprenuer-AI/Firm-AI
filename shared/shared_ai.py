import json
import boto3
import google.generativeai as genai

_api_key = None

def _get_api_key() -> str:
    global _api_key
    if _api_key is None:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        raw = client.get_secret_value(SecretId='rcm/gemini/api-key')['SecretString']
        _api_key = json.loads(raw)['api_key']
    return _api_key

def call_gemini(system_prompt: str, user_message: str, max_chars: int = 1600) -> str:
    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel(
        model_name='gemini-2.0-flash',
        system_instruction=system_prompt
    )
    response = model.generate_content(user_message)
    text = response.text or ''
    return text[:max_chars]

def load_prompt_from_s3(practice_area: str, prompt_name: str = 'intake_v1') -> str:
    s3 = boto3.client('s3', region_name='us-east-2')
    key = f"prompts/{practice_area}/{prompt_name}.txt"
    obj = s3.get_object(Bucket='firmos-documents-006619321854', Key=key)
    return obj['Body'].read().decode('utf-8')
