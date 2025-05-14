import os
import sys
import json
import re
import time
from datetime import datetime
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import Select
from pydantic import BaseModel
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
import logging
import asyncio
from selenium.webdriver.chrome.options import Options
import base64
import fitz  # PyMuPDF for PDF-to-PNG conversion

# Create a directory for storing PNG files
STATIC_DIR = os.path.join(os.getcwd(), "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

# Initialize FastAPI app
app = FastAPI(
    title="IRS EIN API",
    description="API for receiving JSON data and applying for IRS EIN with manual submission decision and PNG output",
    version="1.0.0"
)

# Mount the static directory to serve files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get('PORT', 8000))

# Define a consistent file path for the JSON data
JSON_FILE_PATH = os.path.join(os.getcwd(), "salesforce_data.json")

# Store browser sessions by record_id
browser_sessions = {}

# Pydantic model for request body (all fields are optional except record_id)
class CaseData(BaseModel):
    record_id: str  # Required field
    entity_name: Optional[str] = None
    entity_type: Optional[str] = None
    formation_date: Optional[str] = None
    business_category: Optional[str] = None
    business_description: Optional[str] = None
    business_address_1: Optional[str] = None
    entity_state: Optional[str] = None
    business_address_2: Optional[str] = None
    city: Optional[str] = None
    zip_code: Optional[str] = None
    quarter_of_first_payroll: Optional[str] = None
    entity_state_record_state: Optional[str] = None
    json_summary: Optional[dict] = None
    summary_raw: Optional[str] = None
    case_contact_name: Optional[str] = None
    ssn_decrypted: Optional[str] = None
    case_contact_first_name: Optional[str] = None
    case_contact_last_name: Optional[str] = None
    case_contact_phone: Optional[str] = None
    proceed_flag: Optional[str] = "true"

# Pydantic model for confirmation response from Postman
class ConfirmationData(BaseModel):
    formId: str
    proceed: bool

# Pydantic model for submit decision
class SubmitDecision(BaseModel):
    record_id: str
    proceed: bool

# Helper Functions
def export_to_json_direct(data, json_file_path):
    if not data:
        logger.warning("No data to export.")
        return False
    
    logger.info(f"Attempting to create JSON file: {json_file_path}")
    
    directory = os.path.dirname(json_file_path)
    try:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info(f"Created directory: {directory}")
        else:
            logger.info(f"Directory already exists: {directory}")
            
        test_file = os.path.join(directory, "test_write.txt")
        try:
            with open(test_file, 'w') as f:
                f.write("Test")
            logger.info(f"Successfully created test file: {test_file}")
            os.remove(test_file)
            logger.info("Test file removed successfully")
        except Exception as e:
            logger.error(f"Failed to write test file: {e}")
            return False
    except Exception as e:
        logger.error(f"Failed to access or create directory: {e}")
        return False
    
    try:
        existing_data = []
        if os.path.exists(json_file_path):
            with open(json_file_path, 'r', encoding='utf-8') as f:
                try:
                    existing_data = json.load(f)
                    if not isinstance(existing_data, list):
                        existing_data = [existing_data]
                except json.JSONDecodeError:
                    logger.warning(f"Existing JSON file {json_file_path} is invalid, overwriting with new data.")
                    existing_data = []
        
        existing_data.append(data)
        
        with open(json_file_path, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=2)
        
        logger.info(f"SUCCESS: Data appended to JSON file at {json_file_path}")
        file_size = os.path.getsize(json_file_path)
        logger.info(f"File size: {file_size} bytes")
        return True
    except Exception as e:
        logger.error(f"Failed to write to JSON file: {e}")
        return False

def try_multiple_locations(data):
    global JSON_FILE_PATH
    if export_to_json_direct(data, JSON_FILE_PATH):
        return True
    
    locations = [
        os.path.join(os.getcwd(), "salesforce_data.json"),
        os.path.join(os.path.expanduser("~"), "Desktop", "salesforce_data.json"),
        os.path.join(os.path.expanduser("~"), "Documents", "salesforce_data.json"),
        os.path.join(os.environ.get('TEMP', '/tmp'), "salesforce_data.json"),
        "salesforce_data.json"
    ]
    
    success = False
    for location in locations:
        logger.info(f"Trying location: {location}")
        if export_to_json_direct(data, location):
            logger.info(f"SUCCESS: Found working location at {location}")
            JSON_FILE_PATH = location
            success = True
            break
    
    if not success:
        logger.error("FAILED: Could not write to any locations.")
    
    return success

def determine_number_of_members(json_summary):
    if not json_summary:
        logger.info("No json_summary provided, defaulting to 2 members.")
        return 2
    
    try:
        responsible_parties = set()
        
        def search_responsible_parties(data):
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(key, str) and "responsible party-" in key.lower():
                        party_num = key.lower().split("responsible party-")[-1].split()[0]
                        responsible_parties.add(party_num)
                    if isinstance(value, (dict, list)):
                        search_responsible_parties(value)
            elif isinstance(data, list):
                for item in data:
                    search_responsible_parties(item)
        
        search_responsible_parties(json_summary)
        
        if not responsible_parties:
            logger.info("No responsible parties found in json_summary, defaulting to 2 members.")
            return 2
        
        max_party = max(int(num) for num in responsible_parties)
        if max_party >= 1 and max_party <= 4:
            logger.info(f"Found responsible parties up to {max_party}, setting number of members to {max_party}.")
            return max_party
        else:
            logger.info(f"Unexpected number of responsible parties ({max_party}), defaulting to 2 members.")
            return 2
    except Exception as e:
        logger.error(f"Error processing json_summary: {e}, defaulting to 2 members.")
        return 2

# IRS EIN Application Functions
def fill_field(driver, field, value, label):
    if value is None or value.strip() == "":
        logger.warning(f"Skipping {label} as value is None or empty")
        return
    logger.info(f"Filling {label} with: '{value}'")
    driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", field)
    time.sleep(0.2)
    field.click()
    field.clear()
    field.send_keys(str(value))
    filled_value = field.get_attribute("value")
    logger.info(f"Verification - Filled {label}: '{filled_value}'")

def handle_unexpected_popups(driver):
    try:
        driver.switch_to.alert.accept()
    except:
        pass
    
    try:
        no_thanks_button = driver.find_element(By.XPATH, "//button[contains(text(), 'No thanks')]")
        no_thanks_button.click()
    except:
        pass

    driver.execute_script("""
        window.alert = function() { return true; };
        window.confirm = function() { return true; };
        window.prompt = function() { return null; };
        window.open = function() { return null; };
    """)

state_mapping = {
    "ALABAMA": "AL", "AL": "AL", "ALABAMA (AL)": "AL",
    "ALASKA": "AK", "AK": "AK", "ALASKA (AK)": "AK",
    "ARIZONA": "AZ", "AZ": "AZ", "ARIZONA (AZ)": "AZ",
    "ARKANSAS": "AR", "AR": "AR", "ARKANSAS (AR)": "AR",
    "CALIFORNIA": "CA", "CA": "CA", "CALIFORNIA (CA)": "CA",
    "COLORADO": "CO", "CO": "CO", "COLORADO (CO)": "CO",
    "CONNECTICUT": "CT", "CT": "CT", "CONNECTICUT (CT)": "CT",
    "DELAWARE": "DE", "DE": "DE", "DELAWARE (DE)": "DE",
    "DISTRICT OF COLUMBIA": "DC", "DC": "DC", "DISTRICT OF COLUMBIA (DC)": "DC",
    "FLORIDA": "FL", "FL": "FL", "FLORIDA (FL)": "FL",
    "GEORGIA": "GA", "GA": "GA", "GEORGIA (GA)": "GA",
    "HAWAII": "HI", "HI": "HI", "HAWAII (HI)": "HI",
    "IDAHO": "ID", "ID": "ID", "IDAHO (ID)": "ID",
    "ILLINOIS": "IL", "IL": "IL", "ILLINOIS (IL)": "IL",
    "INDIANA": "IN", "IN": "IN", "INDIANA (IN)": "IN",
    "IOWA": "IA", "IA": "IA", "IOWA (IA)": "IA",
    "KANSAS": "KS", "KS": "KS", "KANSAS (KS)": "KS",
    "KENTUCKY": "KY", "KY": "KY", "KENTUCKY (KY)": "KY",
    "LOUISIANA": "LA", "LA": "LA", "LOUISIANA (LA)": "LA",
    "MAINE": "ME", "ME": "ME", "MAINE (ME)": "ME",
    "MARYLAND": "MD", "MD": "MD", "MARYLAND (MD)": "MD",
    "MASSACHUSETTS": "MA", "MA": "MA", "MASSACHUSETTS (MA)": "MA",
    "MICHIGAN": "MI", "MI": "MI", "MICHIGAN (MI)": "MI",
    "MINNESOTA": "MN", "MN": "MN", "MINNESOTA (MN)": "MN",
    "MISSISSIPPI": "MS", "MS": "MS", "MISSISSIPPI (MS)": "MS",
    "MISSOURI": "MO", "MO": "MO", "MISSOURI (MO)": "MO",
    "MONTANA": "MT", "MT": "MT", "MONTANA (MT)": "MT",
    "NEBRASKA": "NE", "NE": "NE", "NEBRASKA (NE)": "NE",
    "NEVADA": "NV", "NV": "NV", "NEVADA (NV)": "NV",
    "NEW HAMPSHIRE": "NH", "NH": "NH", "NEW HAMPSHIRE (NH)": "NH",
    "NEW JERSEY": "NJ", "NJ": "NJ", "NEW JERSEY (NJ)": "NJ",
    "NEW MEXICO": "NM", "NM": "NM", "NEW MEXICO (NM)": "NM",
    "NEW YORK": "NY", "NY": "NY", "NEW YORK (NY)": "NY",
    "NORTH CAROLINA": "NC", "NC": "NC", "NORTH CAROLINA (NC)": "NC",
    "NORTH DAKOTA": "ND", "ND": "ND", "NORTH DAKOTA (ND)": "ND",
    "OHIO": "OH", "OH": "OH", "OHIO (OH)": "OH",
    "OKLAHOMA": "OK", "OK": "OK", "OKLAHOMA (OK)": "OK",
    "OREGON": "OR", "OR": "OR", "OREGON (OR)": "OR",
    "PENNSYLVANIA": "PA", "PA": "PA", "PENNSYLVANIA (PA)": "PA",
    "RHODE ISLAND": "RI", "RI": "RI", "RHODE ISLAND (RI)": "RI",
    "SOUTH CAROLINA": "SC", "SC": "SC", "SOUTH CAROLINA (SC)": "SC",
    "SOUTH DAKOTA": "SD", "SD": "SD", "SOUTH DAKOTA (SD)": "SD",
    "TENNESSEE": "TN", "TN": "TN", "TENNESSEE (TN)": "TN",
    "TEXAS": "TX", "TX": "TX", "TEXAS (TX)": "TX",
    "UTAH": "UT", "UT": "UT", "UTAH (UT)": "UT",
    "VERMONT": "VT", "VT": "VT", "VERMONT (VT)": "VT",
    "VIRGINIA": "VA", "VA": "VA", "VIRGINIA (VA)": "VA",
    "WASHINGTON": "WA", "WA": "WA", "WASHINGTON (WA)": "WA",
    "WEST VIRGINIA": "WV", "WV": "WV", "WEST VIRGINIA (WV)": "WV",
    "WISCONSIN": "WI", "WI": "WI", "WISCONSIN (WI)": "WI",
    "WYOMING": "WY", "WY": "WY", "WYOMING (WY)": "WY"
}

def select_state(driver, physical_state):
    if not physical_state:
        logger.warning("physical_state is missing, defaulting to 'TX'")
        physical_state = "TX"
    
    try:
        state_select = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "state")))
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", state_select)
        
        select = Select(state_select)
        available_values = [option.get_attribute("value") for option in select.options]
        
        physical_state = physical_state.upper().strip()
        
        if physical_state in available_values:
            state_value = physical_state
        else:
            normalized_input = re.sub(r'\s*\([^)]+\)', '', physical_state).strip()
            state_value = state_mapping.get(physical_state, state_mapping.get(normalized_input))
            
            if not state_value or state_value not in available_values:
                logger.warning(f"Invalid state: '{physical_state}', defaulting to 'TX'")
                state_value = "TX"
        
        try:
            select.select_by_value(state_value)
        except Exception:
            try:
                display_text = next(text for text, val in zip([o.text for o in select.options], available_values) if val == state_value)
                select.select_by_visible_text(display_text)
            except Exception:
                driver.execute_script(f"arguments[0].value = '{state_value}';", state_select)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", state_select)
                
        handle_unexpected_popups(driver)
    except Exception as e:
        logger.warning(f"Failed to select state with input '{physical_state}': {e}, defaulting to 'TX'")
        try:
            select = Select(state_select)
            select.select_by_value("TX")
        except:
            logger.error("Failed to default state to 'TX'")

def click_button(driver, wait, locator, desc="button", scroll=True, retries=2):
    for attempt in range(retries + 1):
        try:
            button = wait.until(EC.element_to_be_clickable(locator))
            if scroll:
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", button)
                time.sleep(0.2)
            button.click()
            logger.info(f"Successfully clicked the '{desc}'")
            time.sleep(0.5)
            handle_unexpected_popups(driver)
            return True
        except Exception as e:
            if attempt == retries:
                logger.warning(f"Failed to click '{desc}': {e}")
                return False
            time.sleep(0.5)

def select_radio(driver, wait, radio_id, desc="radio button", retry=1):
    try:
        driver.execute_script(f"document.getElementById('{radio_id}').checked = true;")
        if driver.execute_script(f"return document.getElementById('{radio_id}').checked;"):
            logger.info(f"Selected '{desc}' using JavaScript")
            time.sleep(0.3)
            handle_unexpected_popups(driver)
            return True
        
        radio = wait.until(EC.element_to_be_clickable((By.ID, radio_id)))
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", radio)
        radio.click()
        time.sleep(0.5)
        handle_unexpected_popups(driver)
        logger.info(f"Selected '{desc}' by clicking")
        return True
    except Exception as e:
        logger.warning(f"Failed to select '{desc}': {e}")
        return False

async def run_irs_ein_application(data: CaseData):
    # Provide defaults for all fields if they are None
    first_name = data.case_contact_first_name or "Rob"
    last_name = data.case_contact_last_name or "Chuchla"
    ssn_decrypted = data.ssn_decrypted or "123456789"  # Default SSN for testing
    ssn_last_four = ssn_decrypted[-4:] if ssn_decrypted and len(ssn_decrypted) >= 4 else "1234"
    entity_type = data.entity_type or "Limited Liability Company (LLC)"
    quarter_of_first_payroll = data.quarter_of_first_payroll or "03/31/2025"
    formation_date = data.formation_date or "2024-06-24"
    business_category = data.business_category or "Finance"
    business_description = data.business_description or "Financial services"
    legal_business_name = data.entity_name or "Lane Four Capital Partners LLC"
    if not legal_business_name:
        logger.error("entity_name is required but missing")
        raise HTTPException(status_code=400, detail="entity_name is required")
    physical_street1 = data.business_address_1 or "3315 Cherry Ln"
    if not physical_street1:
        logger.error("business_address_1 is required but missing")
        raise HTTPException(status_code=400, detail="business_address_1 is required")
    physical_street2 = data.business_address_2 or ""
    physical_city = data.city or "Austin"
    if not physical_city:
        logger.error("city is required but missing")
        raise HTTPException(status_code=400, detail="city is required")
    physical_state = data.entity_state or "TX"
    physical_zipcode = data.zip_code or "78703"
    if not physical_zipcode:
        logger.error("zip_code is required but missing")
        raise HTTPException(status_code=400, detail="zip_code is required")
    select_state_value = data.entity_state_record_state or physical_state or "TX"
    mailing_street1 = physical_street1
    mailing_street2 = physical_street2
    mailing_city = physical_city
    mailing_state = physical_state
    mailing_zipcode = physical_zipcode

    # Log missing fields
    missing_fields = []
    for field_name in data.__dict__:
        if getattr(data, field_name) is None and field_name != "record_id":
            missing_fields.append(field_name)
    if missing_fields:
        logger.info(f"Missing fields: {', '.join(missing_fields)} - using defaults where applicable")

    # Prepare JSON data for logging
    json_data = {
        "record_id": data.record_id,
        "entity_name": data.entity_name,
        "entity_type": data.entity_type,
        "formation_date": data.formation_date,
        "business_category": data.business_category,
        "business_description": data.business_description,
        "business_address_1": data.business_address_1,
        "entity_state": data.entity_state,
        "business_address_2": data.business_address_2,
        "city": data.city,
        "zip_code": data.zip_code,
        "quarter_of_first_payroll": data.quarter_of_first_payroll,
        "entity_state_record_state": data.entity_state_record_state,
        "json_summary": data.json_summary,
        "summary_raw": data.summary_raw,
        "case_contact_name": data.case_contact_name,
        "ssn_decrypted": data.ssn_decrypted,
        "case_contact_first_name": data.case_contact_first_name,
        "case_contact_last_name": data.case_contact_last_name,
        "case_contact_phone": data.case_contact_phone,
        "proceed_flag": data.proceed_flag
    }
    try_multiple_locations(json_data)

    # Initialize browser
    options = uc.ChromeOptions()
    # options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--disable-infobars')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--start-maximized')
    
    prefs = {
        "profile.default_content_setting_values": {
            "popups": 2, "notifications": 2, "geolocation": 2,
        },
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "autofill.profile_enabled": False,
        "autofill.credit_card_enabled": False,
        "password_manager_enabled": False,
        "profile.password_dismissed_save_prompt": True
    }
    options.add_experimental_option("prefs", prefs)

    driver = None
    try:
        driver = uc.Chrome(options=options)
        wait = WebDriverWait(driver, 10)
        actions = ActionChains(driver)
        
        handle_unexpected_popups(driver)
        
        driver.get("https://sa.www4.irs.gov/modiein/individual/index.jsp")
        handle_unexpected_popups(driver)
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @name='submit' and @value='Begin Application >>']"), 
                    "Begin Application button")
        
        wait.until(EC.presence_of_element_located((By.ID, "individual-leftcontent")))

        # Entity type mapping
        entity_type_mapping = {
            "Limited Liability Company (LLC)": "limited",
            "C-Corporation": "corporations",
            "S-Corporation": "corporations",
            "Non-Profit Corporation": "corporations",
            "ProfessionalLimitedLiabilityCompany (PLLC)": "limited",
            "ProfessionalCorporation": "corporations",
            "Sole Proprietorship": "sole",
            "Partnership": "partnerships",
            "LimitedPartnership": "partnerships",
            "LimitedLiabilityPartnership": "partnerships",
            "Corporation": "corporations",
            "GeneralPartnership": "partnerships",
            "Trusteeship": "trusts",
            "LLC": "limited",
            "LLP": "partnerships",
            "LimitedLiabilityCompany": "limited",
            "ProfessionalLimitedLiabilityCompany": "limited",
            "Estate": "estate"
        }
        
        entity_type_normalized = entity_type.strip() if entity_type else "Limited Liability Company (LLC)"
        if entity_type_normalized not in entity_type_mapping:
            entity_type_normalized = entity_type_normalized.replace(" ", "").replace("(", "").replace(")", "")
        
        mapped_value = entity_type_mapping.get(entity_type_normalized, "limited")
        select_radio(driver, wait, mapped_value, f"entity type {mapped_value}")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        llc_members = determine_number_of_members(data.json_summary)
        
        try:
            llc_members_field = wait.until(EC.element_to_be_clickable((By.ID, "numbermem")))
            fill_field(driver, llc_members_field, str(llc_members), "LLC Members")
        except Exception as e:
            logger.warning(f"Failed to fill LLC Members field: {e}")
        
        select_state(driver, select_state_value)
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        if llc_members == 2:
            select_radio(driver, wait, "radio_n", "radio_n option")
            click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        select_radio(driver, wait, "newbiz", "newbiz option")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        try:
            first_name_field = wait.until(EC.element_to_be_clickable((By.ID, "responsiblePartyFirstName")))
            fill_field(driver, first_name_field, first_name, "First Name")
            
            last_name_field = wait.until(EC.element_to_be_clickable((By.ID, "responsiblePartyLastName")))
            fill_field(driver, last_name_field, last_name, "Last Name")
            
            ssn3_field = wait.until(EC.element_to_be_clickable((By.ID, "responsiblePartySSN3")))
            ssn_first_three = ssn_decrypted[:3] if len(ssn_decrypted) >= 3 else "123"
            fill_field(driver, ssn3_field, ssn_first_three, "SSN3")

            ssn_decrypted = ssn_decrypted.replace('-','')
            ssn2_field = wait.until(EC.element_to_be_clickable((By.ID, "responsiblePartySSN2")))
            ssn_middle_two = ssn_decrypted[3:5] if len(ssn_decrypted) >= 5 else "45"
            fill_field(driver, ssn2_field, ssn_middle_two, "SSN2")
            
            ssn4_field = wait.until(EC.element_to_be_clickable((By.ID, "responsiblePartySSN4")))
            fill_field(driver, ssn4_field, ssn_last_four, "SSN4")
        except Exception as e:
            logger.warning(f"Failed to fill responsible party fields: {e}")
        
        select_radio(driver, wait, "iamsole", "iamsole option")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        try:
            street_field = wait.until(EC.element_to_be_clickable((By.ID, "physicalAddressStreet")))
            fill_field(driver, street_field, physical_street1, "Physical Street Address")
            
            city_field = wait.until(EC.element_to_be_clickable((By.ID, "physicalAddressCity")))
            fill_field(driver, city_field, physical_city, "Physical City")
            
            state_select = wait.until(EC.element_to_be_clickable((By.ID, "physicalAddressState")))
            select = Select(state_select)
            physical_state_normalized = physical_state.upper().strip()
            state_value = state_mapping.get(physical_state_normalized, physical_state_normalized)
            select.select_by_value(state_value)
            
            zip_field = wait.until(EC.element_to_be_clickable((By.ID, "physicalAddressZipCode")))
            fill_field(driver, zip_field, physical_zipcode, "Physical Zip Code")
            
            phone_number = data.case_contact_phone or "2812173123"
            phone_number = re.sub(r'\D', '', phone_number) if phone_number else "2812173123"
            if len(phone_number) != 10:
                logger.warning(f"Invalid phone number format: {phone_number}, defaulting to 2812173123")
                phone_number = "2812173123"
            
            phone_first3 = phone_number[:3]
            phone_middle3 = phone_number[3:6]
            phone_last4 = phone_number[6:10]
            
            phone_first_field = wait.until(EC.element_to_be_clickable((By.ID, "phoneFirst3")))
            fill_field(driver, phone_first_field, phone_first3, "Phone First 3")
            
            phone_middle_field = wait.until(EC.element_to_be_clickable((By.ID, "phoneMiddle3")))
            fill_field(driver, phone_middle_field, phone_middle3, "Phone Middle 3")
            
            phone_last_field = wait.until(EC.element_to_be_clickable((By.ID, "phoneLast4")))
            fill_field(driver, phone_last_field, phone_last4, "Phone Last 4")
        except Exception as e:
            logger.warning(f"Failed to fill address or phone fields: {e}")
        
        primary_address = (physical_street1, physical_city, physical_state, physical_zipcode)
        mailing_address = (mailing_street1, mailing_city, mailing_state, mailing_zipcode)
        
        addresses_same = all(
            (p == m) or (m == '' and p != '') 
            for p, m in zip(primary_address, mailing_address)
        )
        
        if addresses_same:
            select_radio(driver, wait, "radioAnotherAddress_n", "Same address option")
        else:
            select_radio(driver, wait, "radioAnotherAddress_y", "Different address option")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @name='Submit' and @value='Accept As Entered']"), "Accept As Entered button")
        
        try:
            legal_business_name_cleaned = legal_business_name.strip()
            endings_to_remove = ['Corp', 'Inc', 'LLC', 'LC', 'PLLC', 'PA']
            for ending in endings_to_remove:
                if legal_business_name_cleaned.upper().endswith(ending.upper()):
                    legal_business_name_cleaned = legal_business_name_cleaned[:-(len(ending))].strip()
            legal_business_name_cleaned = re.sub(r'[^\w\s\-&]', '', legal_business_name_cleaned)
            
            business_name_field = wait.until(EC.element_to_be_clickable((By.ID, "businessOperationalLegalName")))
            fill_field(driver, business_name_field, legal_business_name_cleaned, "Legal Business Name")
        except Exception as e:
            logger.warning(f"Failed to fill Legal Business Name field with '{legal_business_name_cleaned}': {e}")
        
        try:
            county_field = wait.until(EC.element_to_be_clickable((By.ID, "businessOperationalCounty")))
            fill_field(driver, county_field, physical_city, "Business Operational County")
        except Exception as e:
            logger.warning(f"Failed to fill Business Operational County with '{physical_city}': {e}")
        
        try:
            state_select = wait.until(EC.element_to_be_clickable((By.ID, "articalsFiledState")))
            select = Select(state_select)
            physical_state_normalized = physical_state.upper().strip()
            state_value = state_mapping.get(physical_state_normalized, physical_state_normalized)
            select.select_by_value(state_value)
            logger.info(f"Selected Articles Filed State with value '{state_value}'")
        except Exception as e:
            logger.warning(f"Failed to select Articles Filed State with '{physical_state}': {e}")
        
        try:
            logger.info(f"Raw formation_date: '{formation_date}'")
            date_formats = ["%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"]
            parsed_date = None
            for date_format in date_formats:
                try:
                    parsed_date = datetime.strptime(formation_date.strip(), date_format)
                    logger.info(f"Successfully parsed date with format {date_format}: {parsed_date}")
                    break
                except ValueError:
                    continue
            
            if parsed_date is None:
                logger.warning(f"Could not parse formation_date '{formation_date}', defaulting to 2024-06-24")
                parsed_date = datetime.strptime("2024-06-24", "%Y-%m-%d")
            
            formation_month = parsed_date.month
            formation_year = parsed_date.year
            
            month_select = wait.until(EC.element_to_be_clickable((By.ID, "BUSINESS_OPERATIONAL_MONTH_ID")))
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'auto', block: 'center'});", month_select)
            time.sleep(0.5)
            select = Select(month_select)
            month_value = str(formation_month)
            select.select_by_value(month_value)
            logger.info(f"Selected formation month: {month_value}")
            
            year_input = wait.until(EC.element_to_be_clickable((By.ID, "BUSINESS_OPERATIONAL_YEAR_ID")))
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'auto', block: 'center'});", year_input)
            time.sleep(0.5)
            year_input.clear()
            year_value = str(formation_year)
            year_input.send_keys(year_value)
            logger.info(f"Entered formation year: {year_value}")
            driver.execute_script("arguments[0].blur();", year_input)
        except Exception as e:
            logger.warning(f"Error setting formation date: {e}")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        radio_buttons = [
            ("radioTrucking_n", "Trucking radio (No)"),
            ("radioInvolveGambling_n", "Involve Gambling radio (No)"),
            ("radioExciseTax_n", "Excise Tax radio (No)"),
            ("radioSellTobacco_n", "Sell Tobacco radio (No)"),
            ("radioHasEmployees_n", "Has Employees radio (No)")
        ]
        for radio_id, desc in radio_buttons:
            select_radio(driver, wait, radio_id, desc)
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        select_radio(driver, wait, "other", "Other principal activity radio")
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        select_radio(driver, wait, "other", "Other principal service radio")
        
        try:
            business_description = business_description or "Any and all lawful business"
            specify_field = wait.until(EC.element_to_be_clickable((By.ID, "pleasespecify")))
            fill_field(driver, specify_field, business_description, "Please Specify Business Description")
        except Exception as e:
            logger.warning(f"Failed to fill Please Specify field with '{business_description}': {e}")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        select_radio(driver, wait, "receiveonline", "Receive Online radio")
        
        click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Continue >>']"), "Continue button")
        
        # Print the page as a PDF and convert to PNG using PyMuPDF
        pdf_path = None
        png_path = None
        png_url = None
        try:
            logger.info("Printing the final page as a PDF")
            pdf_path = os.path.join(os.getcwd(), f"temp_print_{data.record_id}_{int(time.time())}.pdf")
            
            # Use Chrome DevTools Protocol to print the page to PDF
            pdf_data = driver.execute_cdp_cmd("Page.printToPDF", {
                "printBackground": True,
                "preferCSSPageSize": True,
                "marginTop": 0,
                "marginBottom": 0,
                "marginLeft": 0,
                "marginRight": 0,
                "paperWidth": 8.27,
                "paperHeight": 11.69,
                "landscape": False
            })
            pdf_base64 = pdf_data["data"]

            # Save the PDF
            with open(pdf_path, "wb") as f:
                f.write(base64.b64decode(pdf_base64))
            logger.info(f"PDF saved to {pdf_path}")

            # Convert PDF to PNG using PyMuPDF
            logger.info("Converting PDF to PNG using PyMuPDF")
            png_filename = f"print_{data.record_id}_{int(time.time())}.png"
            png_path = os.path.join(STATIC_DIR, png_filename)
            
            # Open the PDF with PyMuPDF
            pdf_document = fitz.open(pdf_path)
            
            # Get the first page (page numbering starts at 0)
            page = pdf_document.load_page(0)
            
            # Render the page as a pixmap (image)
            # zoom_x and zoom_y control the resolution (2.0 means 2x scaling for better quality)
            zoom_x = 2.0
            zoom_y = 2.0
            matrix = fitz.Matrix(zoom_x, zoom_y)
            pix = page.get_pixmap(matrix=matrix)
            
            # Save the pixmap as a PNG
            pix.save(png_path)
            logger.info(f"PNG saved to {png_path}")
            
            # Generate a URL for the PNG
            host_url = os.getenv("HOST_URL", "http://localhost:8000")
            png_url = f"{host_url}/static/{png_filename}"
            logger.info(f"PNG URL: {png_url}")
            
            # Close the PDF document
            pdf_document.close()

            # Clean up temporary PDF
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
                logger.info(f"Temporary PDF {pdf_path} removed")
        except Exception as e:
            logger.error(f"Failed to print PDF or convert to PNG: {e}")
            png_path = None
            png_url = None
        
        logger.info("Form submission process reached the final page")
        return driver, wait, True, "IRS EIN application process reached the final page, awaiting manual submission decision", png_path, png_url
    except Exception as e:
        logger.error(f"Error in run_irs_ein_application: {e}")
        if driver:
            try:
                driver.quit()
                logger.info("Browser closed due to error.")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
        return None, None, False, str(e), None, None

# FastAPI Endpoints
@app.post("/run-irs-ein")
async def run_irs_ein_application_endpoint(data: dict, authorization: str = Header(None)):
    expected_api_key = os.getenv("API_KEY", "tX9vL2kQwRtY7uJmK3vL8nWcXe5HgH3v")
    if authorization != f"Bearer {expected_api_key}":
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Extract Form_Automation__c data from the incoming JSON
    form_automation_data = data.get("Form_Automation__c", {})
    if not form_automation_data:
        raise HTTPException(status_code=400, detail="Form_Automation__c data is required")

    # Map JSON data to CaseData fields
    case_data_dict = {
        "record_id": "temp_record_id",  # Placeholder
        "entity_name": form_automation_data.get("Entity__r", {}).get("Name"),
        "entity_type": form_automation_data.get("Entity__r", {}).get("Entity_Type__c"),
        "formation_date": form_automation_data.get("Entity__r", {}).get("Formation_Date__c"),
        "business_category": form_automation_data.get("Entity__r", {}).get("Business_Category__c"),
        "business_description": form_automation_data.get("Entity__r", {}).get("Business_Description__c"),
        "business_address_1": form_automation_data.get("Entity__r", {}).get("Business_Address_1__c"),
        "entity_state": form_automation_data.get("Entity_State__r", {}).get("State__c"),
        "city": form_automation_data.get("Entity__r", {}).get("City__c"),
        "zip_code": form_automation_data.get("Entity__r", {}).get("Zip_Code__c"),
        "json_summary": form_automation_data.get("Case__r", {}).get("JSON_Summary__c"),
        "ssn_decrypted": form_automation_data.get("Contact__r", {}).get("SSN_Decrypted__c"),
        "case_contact_first_name": form_automation_data.get("Entity_Member__r", {}).get("FirstName__c"),
        "case_contact_last_name": form_automation_data.get("Entity_Member__r", {}).get("LastName__c"),
        "case_contact_phone": form_automation_data.get("Entity_Member__r", {}).get("Phone__c"),
        "business_address_2": None,
        "quarter_of_first_payroll": None,
        "entity_state_record_state": None,
        "summary_raw": None,
        "case_contact_name": None,
        "proceed_flag": form_automation_data.get("proceed_flag", "true")
    }

    # Validate and create CaseData instance
    try:
        case_data = CaseData(**case_data_dict)
    except Exception as e:
        logger.error(f"Failed to validate CaseData: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid data format: {str(e)}")

    driver = None
    wait = None
    try:
        # Run the automation up to the final page
        driver, wait, success, message, png_path, png_url = await run_irs_ein_application(case_data)
        if not driver or not wait:
            raise HTTPException(status_code=500, detail="Failed to initialize browser")

        # Store browser session
        browser_sessions[case_data.record_id] = (driver, wait)
        logger.info(f"Stored browser session for record_id: {case_data.record_id}")

        # Send PNG URL to Postman mock server
        postman_endpoint = "https://c89496b5-c613-41c4-b6f9-ae647d74262b.mock.pstmn.io/screenshot"
        status = "Completed" if success else "Failed"
        completion_payload = {
            "record_id": case_data.record_id,
            "status": status,
            "message": message,
            "proceed_flag": case_data.proceed_flag,
            "png_url": png_url
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    postman_endpoint,
                    json=completion_payload
                )
                if response.status_code != 200:
                    logger.error(f"Failed to send PNG URL to Postman endpoint: {response.text}")
                    raise HTTPException(status_code=500, detail=f"Failed to send PNG URL to Postman endpoint: {response.text}")
                logger.info("Successfully sent PNG URL to Postman endpoint")

                # Log and preprocess the response
                logger.info(f"Raw response from Postman mock server: {response.text}")
                response_text = response.text.strip()
                response_text = response_text.replace("'", '"')
                response_text = re.sub(r'([{,]\s*)(\w+)(:)', r'\1"\2"\3', response_text)
                response_text = re.sub(r',\s*([}\]])', r'\1', response_text)
                response_text = re.sub(r'//.*?\n|/\*.*?\*/', '', response_text, flags=re.DOTALL)
                logger.info(f"Preprocessed response: {response_text}")

                try:
                    confirmation_data = json.loads(response_text)
                except json.JSONDecodeError as parse_error:
                    logger.error(f"Failed to parse preprocessed response as JSON: {parse_error}")
                    raise HTTPException(status_code=500, detail=f"Invalid JSON response from Postman endpoint: {parse_error}")
                
                confirmation = ConfirmationData(**confirmation_data)
                logger.info(f"Received confirmation from Postman: formId={confirmation.formId}, proceed={confirmation.proceed}")
            except Exception as e:
                logger.error(f"Error sending PNG URL to Postman endpoint or parsing confirmation: {e}")
                raise HTTPException(status_code=500, detail=f"Error with Postman endpoint: {str(e)}")

        # Start timeout task
        async def timeout_browser(record_id, timeout_seconds=300):
            await asyncio.sleep(timeout_seconds)
            if record_id in browser_sessions:
                driver, wait = browser_sessions.pop(record_id)
                try:
                    driver.quit()
                    logger.info(f"Browser closed due to timeout for record_id: {record_id}")
                except Exception as e:
                    logger.error(f"Error closing browser on timeout for record_id {record_id}: {e}")

        asyncio.create_task(timeout_browser(case_data.record_id))

        # Prepare the response
        response_data = {
            "message": f"IRS EIN application process reached the final page. Send POST request to /submit-decision with {{'record_id': '{case_data.record_id}', 'proceed': true/false}}",
            "status": status,
            "record_id": case_data.record_id,
            "png_path": png_path if png_path else None,
            "png_url": png_url if png_url else None
        }
        return response_data

    except Exception as e:
        logger.error(f"Error in run_irs_ein_application_endpoint: {e}")
        if driver:
            try:
                driver.quit()
                logger.info("Browser closed due to error.")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
        if case_data.record_id in browser_sessions:
            del browser_sessions[case_data.record_id]
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/submit-decision")
async def submit_decision_endpoint(decision: SubmitDecision):
    if decision.record_id not in browser_sessions:
        raise HTTPException(status_code=404, detail=f"No active browser session for record_id: {decision.record_id}")

    driver, wait = browser_sessions.pop(decision.record_id)
    try:
        if decision.proceed:
            logger.info(f"Received proceed=True for record_id: {decision.record_id}. Clicking Submit button.")
            # success = click_button(driver, wait, (By.XPATH, "//input[@type='submit' and @value='Submit']"), "Final Submit button")
            # if success:
                # message = "Form submitted successfully"
            # else:
                # message = "Failed to submit form"
                # raise HTTPException(status_code=500, detail=message)
        else:
            logger.info(f"Received proceed=False for record_id: {decision.record_id}. Closing browser without submitting.")
            message = "Browser closed without submitting form"
        
        return {"record_id": decision.record_id, "message": message, "status": "Completed" if decision.proceed else "Cancelled"}
    except Exception as e:
        logger.error(f"Error processing submit decision for record_id {decision.record_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            driver.quit()
            logger.info(f"Browser closed for record_id: {decision.record_id}")
        except Exception as e:
            logger.error(f"Error closing browser for record_id {decision.record_id}: {e}")

@app.get("/download-screenshot/{record_id}")
async def download_screenshot(record_id: str):
    # Find the most recent screenshot for this record_id
    png_files = [f for f in os.listdir(STATIC_DIR) if f.startswith(f"print_{record_id}_") and f.endswith(".png")]
    
    if not png_files:
        raise HTTPException(status_code=404, detail=f"No screenshots found for record_id: {record_id}")
    
    # Sort by timestamp (newest first)
    png_files.sort(reverse=True)
    latest_png = os.path.join(STATIC_DIR, png_files[0])
    
    return FileResponse(latest_png, media_type="image/png", filename=png_files[0])

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

