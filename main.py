# main.py
import os
import pandas as pd
import requests
import logging
import openai
import shutil
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlparse
from pydantic import BaseModel

# Load environment variables
load_dotenv()

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
#DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_DIR = "/data"
DEST_DATA_DIR = "/data"
BARCODE_API_KEY = os.getenv("BARCODE_API_KEY")
logger = logging.getLogger("uvicorn.error")

# Auth
VALID_USER = "admin"
VALID_PASS = "secret123"
security = HTTPBasic()

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != VALID_USER or credentials.password != VALID_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return credentials.username

# App init
app = FastAPI(title="PGA Lookup", version="1.0.0", docs_url="/docs")
app.mount("/static", StaticFiles(directory="static"), name="static")

class LookupRequest(BaseModel):
    hs_code: str
    name: str | None = None
    description: str | None = None

class UPCRequest(BaseModel):
    upc: str

def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def ensure_persistent_files():
    files = os.listdir(DATA_DIR)
    for f in files:
        src = os.path.join(DATA_DIR, f)
        dst = os.path.join(DEST_DATA_DIR, f)

        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            print(f"Copied {f} to disk at /data")
        else:
            print(f"{f} already exists on disk, skipping")

ensure_persistent_files()

@app.get("/list-persistent")
def list_persistent():
    return os.listdir("/data")

@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE_DIR, "templates/index.html")) as f:
        return f.read()

@app.get("/index.html", response_class=HTMLResponse)
def index():
    return home()

@app.get("/favicon.ico")
def favicon():
    return FileResponse("static/favicon.ico")

@app.get("/list-data")
def list_data():
    return {"files": os.listdir(DATA_DIR)}

@app.post("/lookup-upc")
def lookup_upc(req: UPCRequest, username: str = Depends(auth)):
    if not BARCODE_API_KEY:
        raise HTTPException(status_code=500, detail="Barcode API key not set")
    url = f"https://api.barcodelookup.com/v3/products?key={BARCODE_API_KEY}&barcode={req.upc}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        p = r.json().get("products", [{}])[0]
        return {
            "name": p.get("product_name", ""),
            "brand": p.get("brand", ""),
            "description": p.get("description", ""),
            "image": p.get("images", [""])[0]
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error: {e}")

@app.post("/lookup")
def lookup(req: LookupRequest, username: str = Depends(auth)):
    target = req.hs_code
    chapter_key = target[:2].zfill(2)

    # === HS Chapters ===
    df_chapters = pd.read_excel(f"{DATA_DIR}/HS_Chapters_lookup.xlsx", dtype=str, usecols=["Chapter", "Description"])
    df_chapters["Chapter"] = df_chapters["Chapter"].str.zfill(2)
    chapters = df_chapters[df_chapters["Chapter"] == chapter_key].to_dict("records")

    # === PGA HTS Filtered by HS Code ===
    filtered_hts = pd.read_excel(
        f"{DATA_DIR}/PGA_HTS.xlsx",
        dtype=str,
        usecols=["HTS Number - Full", "PGA Name Code", "PGA Flag Code", "PGA Program Code"],
        engine="openpyxl"
    )
    filtered_hts.columns = filtered_hts.columns.str.strip()
    filtered_hts = filtered_hts[filtered_hts["HTS Number - Full"] == target]
    if filtered_hts.empty:
        return {
            "hs_chapters": chapters,
            "pga_hts": [],
            "pga_sections": {},
            "hs_rules": [],
            "pga_requirements": [],
            "disclaimer": "No PGA data found for given HS Code"
        }

    # === PGA Codes Filtered on Join Keys ===
    df_pga = pd.read_excel(f"{DATA_DIR}/PGA_codes.xlsx", dtype=str).replace("", pd.NA)
    df_pga.columns = df_pga.columns.str.strip()

    # Inner join manually using filtering
    merge_keys = ["PGA Name Code", "PGA Flag Code", "PGA Program Code"]
    merged = pd.merge(
        filtered_hts,
        df_pga,
        how="left",
        left_on=["PGA Name Code", "PGA Flag Code", "PGA Program Code"],
        right_on=["Agency Code", "Code", "Program Code"]
    )
    pga_hts = merged.dropna(axis=1, how="all").to_dict("records")

    # === Extract selected columns to display ===
    pga_sections = {}
    section_cols = [
        "R= Required\n M = May be required", "Tariff Flag Code Definition",
        "PGA Compliance Message (see final in shared google drive) ", "Summary of Requirements",
        "Conditions to Disclaim", "List of Documents Required", "Links to Example Documents",
        "Applicable HTS Codes", "Guidance", "Link to Disclaimer Form Template",
        "CFR Link", "Website Link"
    ]
    for col in section_cols:
        vals = [r[col] for r in pga_hts if col in r and pd.notna(r[col])]
        if vals:
            pga_sections[col.strip()] = list(set(map(str.strip, vals)))

    # === HS Rules - filtered during read using chunks ===
    hs_rules = []
    #try:
    #    sheets = pd.read_excel(f"{DATA_DIR}/hs_codes.xlsx", sheet_name=None)
    #    for sheet in sheets.values():
    #        df = sheet if isinstance(sheet, pd.DataFrame) else pd.DataFrame(sheet)
    #        df["HsCode"] = df["HsCode"].astype(str)
    #        df["Chapter"] = df["HsCode"].str[:2].str.zfill(2)
    #        if target in df["HsCode"].values:
    #            hs_rules = df[df["HsCode"] == target].dropna(axis=1, how="all").to_dict("records")
    #            break
    #        elif target[:4] in df["HsCode"].values:
    #            hs_rules = df[df["HsCode"].str.startswith(target[:4])].dropna(axis=1, how="all").to_dict("records")
    #            break
    #    if not hs_rules:
    #        hs_rules = df[df["Chapter"] == chapter_key].dropna(axis=1, how="all").to_dict("records")
    #except Exception as e:
    #    logger.error(f"HS Rule filtering failed: {e}")
    #    hs_rules = []

    # ChatGPT prompt per URL
    requirements = []

    #for url in sorted(urls):
    #    try:
    #        text = requests.get(url, timeout=10).text
    #        prompt = f"Product Name: {req.name or 'N/A'}\nProduct Description: {req.description or 'N/A'}\n\nFrom this page, list required documents and conditions.\n\n{text}"
    #        chat = openai.chat.completions.create(
    #            model="gpt-4o-mini",
    #            messages=[
    #                {"role": "system", "content": "You are a customs compliance expert."},
    #                {"role": "user", "content": prompt}
    #            ]
    #        )
    #        msg = chat.choices[0].message.content
    #        requirements.append({"url": url, "parsed_requirements": msg})
    #    except Exception as e:
    #        requirements.append({"url": url, "error": str(e)})

    return {
        "hs_chapters": chapters,
        "pga_hts": pga_hts,
        "pga_sections": pga_sections,
        "hs_rules": hs_rules,
        "pga_requirements": requirements,
        "disclaimer": "Sources: I’ve used the ACE Agency Tariff Code Reference Guide (March 5, 2024), ACE Appendix PGA (December 12, 2024), Federal Register notices (e.g., CPSC expansion, September 9, 2024)"
    }
