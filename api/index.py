from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os

app = FastAPI()

class CaseData(BaseModel):
    record_id: str
    entity_name: Optional[str] = None
    # Include other fields from your original model

@app.post("/run-irs-ein")
async def run_irs_ein_application_endpoint(data: dict, authorization: str = Header(None)):
    expected_api_key = os.getenv("API_KEY", "tX9vL2kQwRtY7uJmK3vL8nWcXe5HgH3v")
    if authorization != f"Bearer {expected_api_key}":
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Process the request data
    form_automation_data = data.get("Form_Automation__c", {})
    if not form_automation_data:
        raise HTTPException(status_code=400, detail="Form_Automation__c data is required")
    
    # For Vercel, you might need to handle browser operations differently
    # Perhaps use a separate service or API for the browser automation part
    
    # Return a response
    return {
        "message": "Request received and being processed",
        "record_id": form_automation_data.get("Entity__r", {}).get("Name", "unknown")
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
