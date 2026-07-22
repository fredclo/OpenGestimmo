from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from models import Base, Asset, DepreciationEntry, AccountingEntry, FiscalYear, Account, Category, CostCenter, CompanyInfo, DegressiveRule
from engine import generate_plan, generate_accounting_entries, build_periods
from dateutil.relativedelta import relativedelta
import pandas as pd
import os, uvicorn, datetime, io, json, re

app = FastAPI()
templates = Jinja2Templates(directory="templates")

DOSSIER_DIR = "dossiers"
CONFIG_FILE = os.path.join(DOSSIER_DIR, "current.json")
engines = {}

def get_active_db_path():
    if not os.path.exists(CONFIG_FILE): return None
    with open(CONFIG_FILE, "r") as f: return json.load(f).get("db_path")

def set_active_db_path(db_path):
    with open(CONFIG_FILE, "w") as f: json.dump({"db_path": db_path}, f)

def clear_active_db():
    if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)

def get_engine():
    db_path = get_active_db_path()
    if not db_path or not os.path.exists(db_path): return None
    if db_path not in engines:
        engines[db_path] = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return engines[db_path]

def get_db():
    engine = get_engine()
    if not engine: yield None
    else:
        db = sessionmaker(bind=engine)()
        try: yield db
        finally: db.close()

def get_context(db: Session):
    company = db.query(CompanyInfo).first()
    current_fy = None
    if company and company.current_fiscal_year_id:
        current_fy = db.query(FiscalYear).filter(FiscalYear.id == company.current_fiscal_year_id).first()
    if not current_fy:
        today = datetime.date.today()
        current_fy = db.query(FiscalYear).filter(FiscalYear.start_date <= today, FiscalYear.end_date >= today).first()
    return company, current_fy

@app.on_event("startup")
def startup():
    os.makedirs(DOSSIER_DIR, exist_ok=True)

@app.middleware("http")
async def check_dossier_open(request: Request, call_next):
    path = request.url.path
    allowed_paths = ["/", "/setup", "/dossier/new", "/dossier/create", "/dossier/open", "/dossier/close", "/backup"]
    if path in allowed_paths or path.startswith("/static"):
        return await call_next(request)
    if not get_active_db_path():
        return RedirectResponse(url="/", status_code=303)
    return await call_next(request)

from sqlalchemy.exc import IntegrityError
@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    return HTMLResponse("<div style='font-family:sans-serif;text-align:center;margin-top:50px;'><h1 style='color:#dc2626;'>Action impossible</h1><p>Cette action est interdite car l'élément est lié à d'autres données.</p><br><a href='/' style='color:#2563eb;'>Retour</a></div>", status_code=400)
@app.exception_handler(ValueError)
async def value_exception_handler(request: Request, exc: ValueError):
    return HTMLResponse("<div style='font-family:sans-serif;text-align:center;margin-top:50px;'><h1 style='color:#dc2626;'>Erreur de saisie</h1><p>Vérifiez vos dates et montants.</p><br><a href='/' style='color:#2563eb;'>Retour</a></div>", status_code=400)

def migrate_db(db: Session, engine):
    inspector = inspect(engine)
    cols_fy = [c['name'] for c in inspector.get_columns('fiscal_years')]
    if 'start_date' not in cols_fy:
        db.execute(text("ALTER TABLE fiscal_years ADD COLUMN start_date DATE"))
        db.execute(text("ALTER TABLE fiscal_years ADD COLUMN end_date DATE"))
        for fy in db.query(FiscalYear).all():
            if fy.year:
                fy.start_date = datetime.date(fy.year, 1, 1)
                fy.end_date = datetime.date(fy.year, 12, 31)
        db.commit()
        
    cols_dep = [c['name'] for c in inspector.get_columns('depreciation_entries')]
    if 'fy_start_date' not in cols_dep:
        db.execute(text("ALTER TABLE depreciation_entries ADD COLUMN fy_start_date DATE"))
        db.commit()
        
    if 'degressive_rules' not in inspector.get_table_names():
        db.execute(text("CREATE TABLE degressive_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, min_duration INTEGER, max_duration INTEGER, coefficient FLOAT)"))
        db.add(DegressiveRule(min_duration=3, max_duration=4, coefficient=1.25))
        db.add(DegressiveRule(min_duration=5, max_duration=6, coefficient=1.75))
        db.add(DegressiveRule(min_duration=7, max_duration=999, coefficient=2.25))
        db.commit()

    cols_asset = [c['name'] for c in inspector.get_columns('assets')]
    if 'method_accounting' not in cols_asset:
        db.execute(text("ALTER TABLE assets ADD COLUMN method_accounting VARCHAR DEFAULT 'linear'"))
        db.commit()

    rules = db.query(DegressiveRule).all()
    all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
    for asset in db.query(Asset).all():
        generate_plan(asset, db, all_fys, rules)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if not db: return templates.TemplateResponse("setup.html", {"request": request, "company": None, "active_page": "dashboard"})
    migrate_db(db, get_engine())
    company, current_fy = get_context(db)
    if not company or not current_fy:
        return templates.TemplateResponse("setup.html", {"request": request, "company": company, "active_page": "dashboard"})
    assets = db.query(Asset).all()
    total_value = sum(a.acquisition_value for a in assets if a.status == 'in_service')
    return templates.TemplateResponse("dashboard.html", {"request": request, "assets": assets, "total_value": total_value, "current_fy": current_fy, "company": company, "active_page": "dashboard"})

@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, db: Session = Depends(get_db)):
    company = None
    if db: company = db.query(CompanyInfo).first()
    return templates.TemplateResponse("setup.html", {"request": request, "company": company, "active_page": "settings"})

@app.post("/dossier/create")
async def create_dossier(name: str = Form(...), siret: str = Form(""), address: str = Form(""), legal_form: str = Form(""), start_date: str = Form(...), end_date: str = Form(...)):
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name).lower()
    db_path = os.path.join(DOSSIER_DIR, f"{safe_name}.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    pcg = [("201000", "Frais d'établissement", "asset"), ("205000", "Concessions, brevets", "asset"), ("211000", "Terrains", "asset"), ("215400", "Matériel industriel", "asset"), ("218300", "Matériel bureau", "asset"), ("218400", "Mobilier", "asset"), ("281540", "Amort. Matériel indus.", "depreciation"), ("281830", "Amort. Matériel bureau", "depreciation"), ("145000", "Amort. dérogatoires", "special"), ("687250", "Dot. dérogatoires", "special"), ("787250", "Reprises dérogatoires", "special"), ("675000", "VCEAC", "special"), ("775000", "PCEAC", "special")]
    for acc in pcg: db.add(Account(account_number=acc[0], label=acc[1], account_type=acc[2]))
    db.add(DegressiveRule(min_duration=3, max_duration=4, coefficient=1.25))
    db.add(DegressiveRule(min_duration=5, max_duration=6, coefficient=1.75))
    db.add(DegressiveRule(min_duration=7, max_duration=999, coefficient=2.25))
    
    sd = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    ed = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    if ed <= sd:
        return HTMLResponse("Erreur : La date de fin doit être postérieure à la date de début.", status_code=400)
    if (ed - sd).days < 27:
        return HTMLResponse("Erreur : L'exercice doit durer au moins 1 mois complet (minimum 28 jours).", status_code=400)
    fy = FiscalYear(year=sd.year, start_date=sd, end_date=ed, is_closed=False)
    db.add(fy); db.commit()
    company = CompanyInfo(name=name, siret=siret, address=address, legal_form=legal_form, current_fiscal_year_id=fy.id)
    db.add(company); db.commit(); db.close()
    
    engines[db_path] = engine
    set_active_db_path(db_path)
    return RedirectResponse(url="/", status_code=303)

@app.post("/dossier/open")
async def open_dossier(file: UploadFile = File(...)):
    safe_name = re.sub(r'[^a-zA-Z0-9_\.]', '_', file.filename).lower()
    db_path = os.path.join(DOSSIER_DIR, safe_name)
    with open(db_path, "wb") as f: f.write(await file.read())
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    engines[db_path] = engine
    set_active_db_path(db_path)
    return RedirectResponse(url="/", status_code=303)

@app.get("/dossier/close")
async def close_dossier():
    clear_active_db()
    return RedirectResponse(url="/", status_code=303)

@app.get("/dossier/new")
async def new_dossier():
    return RedirectResponse(url="/setup", status_code=303)

@app.get("/shutdown")
async def shutdown():
    import threading, time, signal
    def kill_server():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=kill_server).start()
    return HTMLResponse("<div style='font-family:sans-serif;text-align:center;margin-top:50px;'><h1>Open Gestimmo est arrêté.</h1><br><p>Vous pouvez fermer cette page.</p></div>")

# --- PARAMÈTRES ---
@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, db: Session = Depends(get_db)):
    migrate_db(db, get_engine())
    years = db.query(FiscalYear).order_by(FiscalYear.start_date.desc()).all()
    company, current_fy = get_context(db)
    
    next_fy_start = None
    next_fy_end = None
    if years:
        latest_fy = years[0]
        next_fy_start = latest_fy.end_date + datetime.timedelta(days=1)
        next_fy_end = next_fy_start + relativedelta(years=1) - datetime.timedelta(days=1)
        
    return templates.TemplateResponse("settings.html", {"request": request, "years": years, "company": company, "current_fy": current_fy, "next_fy_start": next_fy_start, "next_fy_end": next_fy_end, "active_page": "settings"})

@app.post("/settings/year/add")
async def add_year(db: Session = Depends(get_db), start_date: str = Form(...), end_date: str = Form(...)):
    sd = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    ed = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    
    latest_fy = db.query(FiscalYear).order_by(FiscalYear.start_date.desc()).first()
    if latest_fy:
        expected_start = latest_fy.end_date + datetime.timedelta(days=1)
        if sd != expected_start:
            return HTMLResponse(f"Erreur : Le nouvel exercice doit commencer le lendemain de la fin du précédent, soit le {expected_start.strftime('%d/%m/%Y')}.", status_code=400)
            
    fy = FiscalYear(year=sd.year, start_date=sd, end_date=ed, is_closed=False)
    db.add(fy); db.commit()
    rules = db.query(DegressiveRule).all()
    all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
    for asset in db.query(Asset).all(): generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/settings/year/close/{year_id}")
async def close_year(year_id: int, db: Session = Depends(get_db)):
    y = db.query(FiscalYear).filter(FiscalYear.id == year_id).first()
    if y and not y.is_closed: y.is_closed = True; db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/year/select")
async def select_year(db: Session = Depends(get_db), year_id: int = Form(...)):
    company = db.query(CompanyInfo).first()
    if company: company.current_fiscal_year_id = year_id; db.commit()
    return RedirectResponse(url="/settings", status_code=303)

# --- DEGRESSIF ---
@app.get("/degressive", response_class=HTMLResponse)
async def view_degressive(request: Request, db: Session = Depends(get_db)):
    rules = db.query(DegressiveRule).order_by(DegressiveRule.min_duration).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("degressive.html", {"request": request, "rules": rules, "company": company, "active_page": "settings"})

@app.post("/degressive/add")
async def add_degressive(db: Session = Depends(get_db), min_duration: int = Form(...), max_duration: int = Form(...), coefficient: float = Form(...)):
    db.add(DegressiveRule(min_duration=min_duration, max_duration=max_duration, coefficient=coefficient)); db.commit()
    return RedirectResponse(url="/degressive", status_code=303)

@app.post("/degressive/edit/{rule_id}")
async def edit_degressive(rule_id: int, db: Session = Depends(get_db), min_duration: int = Form(...), max_duration: int = Form(...), coefficient: float = Form(...)):
    r = db.query(DegressiveRule).filter(DegressiveRule.id == rule_id).first()
    if r:
        r.min_duration = min_duration; r.max_duration = max_duration; r.coefficient = coefficient; db.commit()
    return RedirectResponse(url="/degressive", status_code=303)

@app.get("/degressive/delete/{rule_id}")
async def delete_degressive(rule_id: int, db: Session = Depends(get_db)):
    r = db.query(DegressiveRule).filter(DegressiveRule.id == rule_id).first()
    if r: db.delete(r); db.commit()
    return RedirectResponse(url="/degressive", status_code=303)

# --- CATEGORIES (Page dédiée) ---
@app.get("/categories", response_class=HTMLResponse)
async def view_categories(request: Request, db: Session = Depends(get_db)):
    categories = db.query(Category).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("categories.html", {"request": request, "categories": categories, "company": company, "active_page": "settings"})

@app.post("/categories/add")
async def add_category(db: Session = Depends(get_db), name: str = Form(...)):
    if not db.query(Category).filter(Category.name == name).first(): db.add(Category(name=name)); db.commit()
    return RedirectResponse(url="/categories", status_code=303)

@app.post("/categories/edit/{cat_id}")
async def edit_category(cat_id: int, db: Session = Depends(get_db), name: str = Form(...)):
    cat = db.query(Category).filter(Category.id == cat_id).first()
    if cat: cat.name = name; db.commit()
    return RedirectResponse(url="/categories", status_code=303)

@app.get("/categories/delete/{cat_id}")
async def delete_category(cat_id: int, db: Session = Depends(get_db)):
    if not db.query(Asset).filter(Asset.category_id == cat_id).first():
        cat = db.query(Category).filter(Category.id == cat_id).first()
        if cat: db.delete(cat); db.commit()
    return RedirectResponse(url="/categories", status_code=303)

# --- CDC (Page dédiée) ---
@app.get("/cdc", response_class=HTMLResponse)
async def view_cdc(request: Request, db: Session = Depends(get_db)):
    cdc = db.query(CostCenter).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("cdc.html", {"request": request, "cdc": cdc, "company": company, "active_page": "settings"})

@app.post("/cdc/add")
async def add_cdc(db: Session = Depends(get_db), name: str = Form(...)):
    if not db.query(CostCenter).filter(CostCenter.name == name).first(): db.add(CostCenter(name=name)); db.commit()
    return RedirectResponse(url="/cdc", status_code=303)

@app.post("/cdc/edit/{cdc_id}")
async def edit_cdc(cdc_id: int, db: Session = Depends(get_db), name: str = Form(...)):
    cdc = db.query(CostCenter).filter(CostCenter.id == cdc_id).first()
    if cdc: cdc.name = name; db.commit()
    return RedirectResponse(url="/cdc", status_code=303)

@app.get("/cdc/delete/{cdc_id}")
async def delete_cdc(cdc_id: int, db: Session = Depends(get_db)):
    if not db.query(Asset).filter(Asset.cost_center_id == cdc_id).first():
        cdc = db.query(CostCenter).filter(CostCenter.id == cdc_id).first()
        if cdc: db.delete(cdc); db.commit()
    return RedirectResponse(url="/cdc", status_code=303)

@app.get("/company/edit", response_class=HTMLResponse)
async def edit_company_form(request: Request, db: Session = Depends(get_db)):
    company, _ = get_context(db)
    return templates.TemplateResponse("company_edit.html", {"request": request, "company": company, "active_page": "settings"})

@app.post("/company/update")
async def update_company(db: Session = Depends(get_db), name: str = Form(...), siret: str = Form(""), address: str = Form(""), legal_form: str = Form("")):
    company = db.query(CompanyInfo).first()
    if company:
        company.name = name; company.siret = siret; company.address = address; company.legal_form = legal_form; db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/pcg", response_class=HTMLResponse)
async def view_pcg(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.account_number).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("pcg.html", {"request": request, "accounts": accounts, "company": company, "active_page": "settings"})

@app.post("/pcg/add")
async def add_account(db: Session = Depends(get_db), account_number: str = Form(...), label: str = Form(...), account_type: str = Form(...)):
    if not db.query(Account).filter(Account.account_number == account_number).first():
        db.add(Account(account_number=account_number, label=label, account_type=account_type)); db.commit()
    return RedirectResponse(url="/pcg", status_code=303)

@app.post("/pcg/edit/{acct_num}")
async def edit_account(acct_num: str, db: Session = Depends(get_db), label: str = Form(...), account_type: str = Form(...)):
    acc = db.query(Account).filter(Account.account_number == acct_num).first()
    if acc: acc.label = label; acc.account_type = account_type; db.commit()
    return RedirectResponse(url="/pcg", status_code=303)

@app.get("/pcg/delete/{acct_num}")
async def delete_account(acct_num: str, db: Session = Depends(get_db)):
    if not db.query(Asset).filter(Asset.account_number == acct_num).first():
        acc = db.query(Account).filter(Account.account_number == acct_num).first()
        if acc: db.delete(acc); db.commit()
    return RedirectResponse(url="/pcg", status_code=303)

# --- IMMOBILISATIONS ---
@app.get("/assets", response_class=HTMLResponse)
async def list_assets(request: Request, db: Session = Depends(get_db), search: str = "", account: str = "", status: str = ""):
    query = db.query(Asset)
    if search: query = query.filter(Asset.name.ilike(f"%{search}%") | Asset.id.ilike(f"%{search}%"))
    if account: query = query.filter(Asset.account_number == account)
    if status: query = query.filter(Asset.status == status)
    assets = query.all()
    accounts = db.query(Account).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("assets.html", {"request": request, "assets": assets, "accounts": accounts, "company": company, "active_page": "assets"})

@app.get("/assets/new", response_class=HTMLResponse)
async def new_asset_form(request: Request, db: Session = Depends(get_db)):
    accounts = db.query(Account).filter(Account.account_type == 'asset').order_by(Account.account_number).all()
    categories = db.query(Category).all()
    cdc = db.query(CostCenter).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("asset_form.html", {"request": request, "asset": None, "accounts": accounts, "categories": categories, "cdc": cdc, "company": company, "active_page": "assets"})

@app.get("/assets/edit/{asset_id}", response_class=HTMLResponse)
async def edit_asset_form(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    accounts = db.query(Account).filter(Account.account_type == 'asset').order_by(Account.account_number).all()
    categories = db.query(Category).all()
    cdc = db.query(CostCenter).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("asset_form.html", {"request": request, "asset": asset, "accounts": accounts, "categories": categories, "cdc": cdc, "company": company, "active_page": "assets"})

@app.post("/assets/save")
async def save_asset(db: Session = Depends(get_db), id: str = Form(...), name: str = Form(...), account_number: str = Form(...), acquisition_value: float = Form(...), residual_value: float = Form(0.0), is_amortizable: bool = Form(False), acquisition_date: str = Form(...), service_date: str = Form(...), duration_accounting: int = Form(0), duration_fiscal: int = Form(0), method_accounting: str = Form("linear"), method_fiscal: str = Form("linear"), cost_center_id: int = Form(None), category_id: int = Form(None)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    acq_date = datetime.datetime.strptime(acquisition_date, "%Y-%m-%d").date()
    srv_date = datetime.datetime.strptime(service_date, "%Y-%m-%d").date()
    if not (current_fy.start_date <= acq_date <= current_fy.end_date) or not (current_fy.start_date <= srv_date <= current_fy.end_date):
        return HTMLResponse(f"Erreur : Les dates doivent appartenir à l'exercice en cours.", status_code=400)
    asset = db.query(Asset).filter(Asset.id == id).first()
    if not asset: asset = Asset(id=id); db.add(asset)
    asset.name=name; asset.account_number=account_number; asset.acquisition_value=acquisition_value
    asset.residual_value = residual_value if residual_value else 0.0
    asset.is_amortizable = is_amortizable
    asset.acquisition_date=acq_date; asset.service_date=srv_date
    asset.duration_accounting=duration_accounting; asset.duration_fiscal=duration_fiscal
    asset.method_accounting=method_accounting; asset.method_fiscal=method_fiscal
    asset.cost_center_id = cost_center_id if cost_center_id else None
    asset.category_id = category_id if category_id else None
    db.commit()
    rules = db.query(DegressiveRule).all()
    all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
    generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/assets", status_code=303)

@app.get("/assets/delete/{asset_id}")
async def delete_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if asset: db.delete(asset); db.commit()
    return RedirectResponse(url="/assets", status_code=303)

@app.get("/assets/dispose/{asset_id}", response_class=HTMLResponse)
async def dispose_asset_form(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    can_cancel = True
    if asset and asset.disposal_date:
        fy = db.query(FiscalYear).filter(FiscalYear.start_date <= asset.disposal_date, FiscalYear.end_date >= asset.disposal_date).first()
        if fy and fy.is_closed: can_cancel = False
    company, _ = get_context(db)
    return templates.TemplateResponse("dispose_form.html", {"request": request, "asset": asset, "can_cancel": can_cancel, "company": company, "active_page": "assets"})

@app.post("/assets/dispose/{asset_id}")
async def dispose_asset(asset_id: str, db: Session = Depends(get_db), disposal_date: str = Form(...), disposal_price: float = Form(0.0)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if asset:
        _, current_fy = get_context(db)
        disp_date = datetime.datetime.strptime(disposal_date, "%Y-%m-%d").date()
        if current_fy and not (current_fy.start_date <= disp_date <= current_fy.end_date):
            return HTMLResponse(f"Erreur : La date de cession doit appartenir à l'exercice en cours.", status_code=400)
        asset.disposal_date = disp_date; asset.disposal_price = disposal_price; asset.status = 'disposed'
        db.commit()
        rules = db.query(DegressiveRule).all()
        all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
        generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/assets", status_code=303)

@app.get("/assets/cancel_disposal/{asset_id}")
async def cancel_disposal(asset_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if asset and asset.status == 'disposed':
        fy = db.query(FiscalYear).filter(FiscalYear.start_date <= asset.disposal_date, FiscalYear.end_date >= asset.disposal_date).first()
        if not (fy and fy.is_closed):
            asset.disposal_date = None; asset.disposal_price = 0.0; asset.status = 'in_service'
            db.commit()
            rules = db.query(DegressiveRule).all()
            all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
            generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/assets", status_code=303)

@app.get("/assets/recalculate/{asset_id}")
async def recalculate_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if asset:
        rules = db.query(DegressiveRule).all()
        all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
        generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url=f"/assets/plan/{asset_id}", status_code=303)

@app.get("/assets/plan/{asset_id}", response_class=HTMLResponse)
async def view_asset_plan(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
    rules = db.query(DegressiveRule).all()
    timeline = build_periods(asset, all_fys, rules)
    plans = []
    cumul_econ = 0.0
    cumul_fisc = 0.0
    for p in timeline:
        econ = round(p['econ'], 2)
        fisc = round(p['fisc'], 2)
        derog = round(fisc - econ, 2)
        cumul_econ += econ
        cumul_fisc += fisc
        plans.append({
            'start_date': p['start'], 'end_date': p['end'],
            'economic_depreciation': econ, 'fiscal_depreciation': fisc, 'derogatory_depreciation': derog,
            'cumulative_economic': round(cumul_econ, 2), 'cumulative_fiscal': round(cumul_fisc, 2)
        })
    company, _ = get_context(db)
    return templates.TemplateResponse("asset_plan.html", {"request": request, "asset": asset, "plans": plans, "company": company, "active_page": "assets"})

# --- IMPORT / EXPORT DOSSIER ---
@app.get("/export/dossier")
async def export_dossier(db: Session = Depends(get_db)):
    assets = db.query(Asset).all()
    data = []
    for a in assets:
        cat = db.query(Category).filter(Category.id == a.category_id).first()
        cdc = db.query(CostCenter).filter(CostCenter.id == a.cost_center_id).first()
        data.append({"id": a.id, "name": a.name, "account_number": a.account_number, "category": cat.name if cat else "", "cost_center": cdc.name if cdc else "", "acquisition_value": a.acquisition_value, "residual_value": a.residual_value, "is_amortizable": a.is_amortizable, "acquisition_date": a.acquisition_date, "service_date": a.service_date, "duration_accounting": a.duration_accounting, "duration_fiscal": a.duration_fiscal, "method_accounting": a.method_accounting, "method_fiscal": a.method_fiscal, "status": a.status, "disposal_date": a.disposal_date if a.disposal_date else "", "disposal_price": a.disposal_price if a.disposal_price else 0.0})
    df = pd.DataFrame(data)
    output = "export_dossier.xlsx"; df.to_excel(output, index=False)
    with open(output, "rb") as f: content = f.read()
    os.remove(output)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={output}"})

@app.post("/import/dossier")
async def import_dossier(db: Session = Depends(get_db), file: UploadFile = File(...)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    df = pd.read_csv(io.BytesIO(await file.read()))
    for _, row in df.iterrows():
        asset_id = str(row["id"])
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset: asset = Asset(id=asset_id); db.add(asset)
        asset.name = str(row["name"]); asset.account_number = str(row["account_number"]); asset.acquisition_value = float(row["acquisition_value"]); asset.residual_value = float(row.get("residual_value", 0.0)); asset.is_amortizable = bool(row.get("is_amortizable", True)); asset.acquisition_date = pd.to_datetime(row["acquisition_date"]).date(); asset.service_date = pd.to_datetime(row["service_date"]).date(); asset.duration_accounting = int(row["duration_accounting"]); asset.duration_fiscal = int(row["duration_fiscal"]); asset.method_accounting = str(row.get("method_accounting", "linear")); asset.method_fiscal = str(row.get("method_fiscal", "linear")); asset.status = str(row.get("status", "in_service"))
        if pd.notna(row.get("disposal_date")) and str(row.get("disposal_date")) != "":
            asset.disposal_date = pd.to_datetime(row["disposal_date"]).date(); asset.disposal_price = float(row.get("disposal_price", 0.0))
        else: asset.disposal_date = None; asset.disposal_price = 0.0
        cat_name = row.get("category")
        if pd.notna(cat_name) and str(cat_name).strip() != "":
            cat = db.query(Category).filter(Category.name == str(cat_name)).first()
            if not cat: cat = Category(name=str(cat_name)); db.add(cat); db.commit()
            asset.category_id = cat.id
        else: asset.category_id = None
        cdc_name = row.get("cost_center")
        if pd.notna(cdc_name) and str(cdc_name).strip() != "":
            cdc = db.query(CostCenter).filter(CostCenter.name == str(cdc_name)).first()
            if not cdc: cdc = CostCenter(name=str(cdc_name)); db.add(cdc); db.commit()
            asset.cost_center_id = cdc.id
        else: asset.cost_center_id = None
        db.commit()
        rules = db.query(DegressiveRule).all()
        all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
        generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/assets", status_code=303)

@app.get("/import/template")
async def download_template():
    df = pd.DataFrame(columns=["id", "name", "account_number", "category", "cost_center", "acquisition_value", "residual_value", "is_amortizable", "acquisition_date", "service_date", "duration_accounting", "duration_fiscal", "method_accounting", "method_fiscal", "status", "disposal_date", "disposal_price"])
    output = "modele_import_dossier.csv"; df.to_csv(output, index=False)
    with open(output, "rb") as f: content = f.read()
    os.remove(output)
    return Response(content=content, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={output}"})

# --- AMORTISSEMENTS & COMPTA ---
@app.get("/amortization", response_class=HTMLResponse)
async def view_amortization(request: Request, db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    entries = db.query(DepreciationEntry).filter(DepreciationEntry.fy_start_date == current_fy.start_date).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("amortization.html", {"request": request, "entries": entries, "current_fy": current_fy, "company": company, "active_page": "amort"})

@app.post("/amortization/calculate")
async def calc_all_amortization(db: Session = Depends(get_db)):
    rules = db.query(DegressiveRule).all()
    all_fys = db.query(FiscalYear).order_by(FiscalYear.start_date).all()
    for asset in db.query(Asset).all(): generate_plan(asset, db, all_fys, rules)
    return RedirectResponse(url="/amortization", status_code=303)

@app.get("/accounting", response_class=HTMLResponse)
async def view_accounting(request: Request, db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    entries = db.query(AccountingEntry).filter(AccountingEntry.date >= current_fy.start_date, AccountingEntry.date <= current_fy.end_date).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("accounting.html", {"request": request, "entries": entries, "current_fy": current_fy, "company": company, "active_page": "compta"})

@app.post("/accounting/generate")
async def gen_accounting(db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if current_fy: generate_accounting_entries(db, current_fy)
    return RedirectResponse(url="/accounting", status_code=303)

# --- CERFA ---
@app.get("/cerfa", response_class=HTMLResponse)
async def view_cerfa(request: Request, db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    assets = db.query(Asset).all()
    cerfa_data = {}
    for a in assets:
        acq_year = a.acquisition_date.year
        disp_year = a.disposal_date.year if a.disposal_date else 9999
        val_debut = a.acquisition_value if acq_year < current_fy.start_date.year and disp_year != current_fy.start_date.year else 0.0
        augm = a.acquisition_value if acq_year == current_fy.start_date.year else 0.0
        dim = a.acquisition_value if disp_year == current_fy.start_date.year else 0.0
        val_fin = val_debut + augm - dim
        plan_current = db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == a.id, DepreciationEntry.fy_start_date == current_fy.start_date).first()
        plan_before = db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == a.id, DepreciationEntry.fy_start_date < current_fy.start_date).order_by(DepreciationEntry.fy_start_date.desc()).first()
        amort_debut = plan_before.cumulative_economic if plan_before else 0.0
        dotation = plan_current.economic_depreciation if plan_current else 0.0
        reprise = plan_current.cumulative_economic if disp_year == current_fy.start_date.year else 0.0
        amort_fin = amort_debut + dotation - reprise
        cession_val = a.acquisition_value if disp_year == current_fy.start_date.year else 0.0
        cession_amort = reprise
        cession_pv = a.disposal_price if disp_year == current_fy.start_date.year else 0.0
        cession_plus_val = cession_pv - (cession_val - cession_amort)
        if a.account_number not in cerfa_data:
            cerfa_data[a.account_number] = {"val_debut": 0, "augm": 0, "dim": 0, "val_fin": 0, "amort_debut": 0, "dotation": 0, "reprise": 0, "amort_fin": 0, "cess_val": 0, "cess_amort": 0, "cess_pv": 0, "cess_pv_val": 0}
        d = cerfa_data[a.account_number]
        d["val_debut"] += val_debut; d["augm"] += augm; d["dim"] += dim; d["val_fin"] += val_fin
        d["amort_debut"] += amort_debut; d["dotation"] += dotation; d["reprise"] += reprise; d["amort_fin"] += amort_fin
        d["cess_val"] += cession_val; d["cess_amort"] += cession_amort; d["cess_pv"] += cession_pv; d["cess_pv_val"] += cession_plus_val
    company, _ = get_context(db)
    return templates.TemplateResponse("cerfa.html", {"request": request, "cerfa_data": cerfa_data, "current_fy": current_fy, "company": company, "active_page": "cerfa"})

@app.get("/cerfa/detail/{acct_num}", response_class=HTMLResponse)
async def cerfa_detail(acct_num: str, request: Request, db: Session = Depends(get_db), category: str = "", acq_year: str = ""):
    query = db.query(Asset).filter(Asset.account_number == acct_num)
    if category:
        cat = db.query(Category).filter(Category.name == category).first()
        if cat: query = query.filter(Asset.category_id == cat.id)
        else: query = query.filter(Asset.category_id == -1)
    all_assets = query.all()
    if acq_year:
        try: assets = [a for a in all_assets if a.acquisition_date.year == int(acq_year)]
        except: assets = all_assets
    else: assets = all_assets
    categories = db.query(Category).all()
    company, _ = get_context(db)
    return templates.TemplateResponse("cerfa_detail.html", {"request": request, "acct_num": acct_num, "assets": assets, "categories": categories, "sel_category": category, "sel_acq_year": acq_year, "company": company, "active_page": "cerfa"})

# --- EXPORTS EXCEL CONTEXTUELS ---
@app.get("/export/excel/amortization")
async def export_amort_excel(db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    entries = db.query(DepreciationEntry).filter(DepreciationEntry.fy_start_date == current_fy.start_date).all()
    data = []
    for e in entries:
        asset = db.query(Asset).filter(Asset.id == e.asset_id).first()
        data.append({"ID Immo": e.asset_id, "Nom": asset.name if asset else "", "Année": e.fy_start_date.year, "Dotation Econ": e.economic_depreciation, "Dotation Fisc": e.fiscal_depreciation, "Derogatoire": e.derogatory_depreciation, "Cumul Econ": e.cumulative_economic})
    df = pd.DataFrame(data); output = "export_amortissement.xlsx"; df.to_excel(output, index=False)
    with open(output, "rb") as f: content = f.read()
    os.remove(output)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={output}"})

@app.get("/export/excel/accounting")
async def export_accounting_excel(db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    entries = db.query(AccountingEntry).filter(AccountingEntry.date >= current_fy.start_date, AccountingEntry.date <= current_fy.end_date).all()
    data = []
    for e in entries: data.append({"Date": e.date, "Compte Debit": e.account_debit, "Compte Credit": e.account_credit, "Libelle": e.label, "Montant": e.amount})
    df = pd.DataFrame(data); output = "export_comptabilite.xlsx"; df.to_excel(output, index=False)
    with open(output, "rb") as f: content = f.read()
    os.remove(output)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={output}"})

@app.get("/export/excel/cerfa")
async def export_cerfa_excel(db: Session = Depends(get_db)):
    _, current_fy = get_context(db)
    if not current_fy: return RedirectResponse(url="/", status_code=303)
    assets = db.query(Asset).all()
    data = []
    for a in assets:
        plan_current = db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == a.id, DepreciationEntry.fy_start_date == current_fy.start_date).first()
        plan_before = db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == a.id, DepreciationEntry.fy_start_date < current_fy.start_date).order_by(DepreciationEntry.fy_start_date.desc()).first()
        data.append({"Compte": a.account_number, "Val Debut": a.acquisition_value, "Augmentations": 0, "Diminutions": 0, "Val Fin": 0, "Amort Debut": plan_before.cumulative_economic if plan_before else 0, "Dotations": plan_current.economic_depreciation if plan_current else 0, "Reprises": 0, "Amort Fin": 0})
    df = pd.DataFrame(data); output = "export_cerfa.xlsx"; df.to_excel(output, index=False)
    with open(output, "rb") as f: content = f.read()
    os.remove(output)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={output}"})

@app.get("/backup")
async def backup_db(db: Session = Depends(get_db)):
    db_path = get_active_db_path()
    if not db_path or not db: return RedirectResponse(url="/", status_code=303)
    company, _ = get_context(db)
    if company and company.name:
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '', company.name.replace(" ", "_"))
        file_prefix = safe_name[:10] if len(safe_name) > 10 else safe_name
        filename = f"{file_prefix}.db"
    else: filename = "open_gestimmo.db"
    with open(db_path, "rb") as f: content = f.read()
    return Response(content=content, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={filename}"})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
