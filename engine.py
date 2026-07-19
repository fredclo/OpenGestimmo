from models import Asset, DepreciationEntry, AccountingEntry
from sqlalchemy.orm import Session
from datetime import date

def get_degressive_rate(duration: int) -> float:
    if 3 <= duration <= 4: return 1.25
    elif 5 <= duration <= 6: return 1.75
    elif duration > 6: return 2.25
    return 1.0

def calculate_linear(base: float, duration: int, service_date: date, calc_year: int, disposal_date: date = None) -> float:
    if duration <= 0: return 0.0
    annual = base / duration
    start_year = service_date.year
    if calc_year < start_year or calc_year > start_year + duration: return 0.0
    if calc_year == start_year:
        months = 12 - (service_date.month - 1)
        annuity = annual * (months / 12)
    elif calc_year == start_year + duration:
        months = service_date.month - 1
        annuity = annual * (months / 12)
    else:
        annuity = annual
    if disposal_date and calc_year == disposal_date.year:
        months = disposal_date.month
        annuity = annual * (months / 12)
    return round(annuity, 2)

def calculate_degressive(base: float, duration: int, service_date: date, calc_year: int, disposal_date: date = None) -> float:
    if duration <= 0: return 0.0
    rate = get_degressive_rate(duration) / duration
    current_book_value = base
    start_year = service_date.year
    for y in range(start_year, calc_year + 1):
        if y == start_year:
            months = 12 - (service_date.month - 1)
            annuity = current_book_value * rate * (months / 12)
        else:
            remaining_years = duration - (y - start_year)
            if remaining_years <= 0: return 0.0
            linear_rate = 1 / remaining_years
            if linear_rate > rate: annuity = current_book_value * linear_rate
            else: annuity = current_book_value * rate
        
        if disposal_date and y == disposal_date.year:
            months = disposal_date.month
            full_year_annuity = current_book_value * rate
            annuity = full_year_annuity * (months / 12)
            return round(annuity, 2)
            
        if y == calc_year: return round(annuity, 2)
        current_book_value -= annuity
        if current_book_value < 0: current_book_value = 0
    return 0.0

def generate_plan(asset: Asset, db: Session, target_year: int):
    db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == asset.id).delete(synchronize_session=False)
    
    # Si non amortissable, on ne génère aucun plan
    if not asset.is_amortizable:
        db.commit()
        return
    
    residual = asset.residual_value if asset.residual_value else 0.0
    base = asset.acquisition_value - residual
    if base < 0: base = 0
    
    cumul_econ = 0.0
    cumul_fisc = 0.0
    end_year = asset.service_date.year + asset.duration_accounting
    if asset.disposal_date: end_year = asset.disposal_date.year

    for year in range(asset.service_date.year, end_year + 1):
        econ = calculate_linear(base, asset.duration_accounting, asset.service_date, year, asset.disposal_date)
        if asset.method_fiscal == 'degressive':
            fisc = calculate_degressive(base, asset.duration_fiscal, asset.service_date, year, asset.disposal_date)
        else:
            fisc = calculate_linear(base, asset.duration_fiscal, asset.service_date, year, asset.disposal_date)
        
        derog = round(fisc - econ, 2)
        cumul_econ += econ
        cumul_fisc += fisc
        
        db.add(DepreciationEntry(
            asset_id=asset.id, fiscal_year=year,
            economic_depreciation=econ, fiscal_depreciation=fisc, derogatory_depreciation=derog,
            cumulative_economic=round(cumul_econ, 2), cumulative_fiscal=round(cumul_fisc, 2)
        ))
    db.commit()

def generate_accounting_entries(db: Session, fiscal_year: int):
    db.query(AccountingEntry).filter(AccountingEntry.date.like(f"{fiscal_year}-%")).delete()
    assets = db.query(Asset).all()
    for asset in assets:
        if not asset.is_amortizable: continue # Ignore si non amortissable
        
        plan = db.query(DepreciationEntry).filter(
            DepreciationEntry.asset_id == asset.id, DepreciationEntry.fiscal_year == fiscal_year
        ).first()
        if not plan: continue
        
        if plan.economic_depreciation > 0:
            db.add(AccountingEntry(
                date=date(fiscal_year, 12, 31), account_debit="6811", account_credit="28" + asset.account_number[1:],
                label=f"Dot. Econ. {asset.name}", amount=plan.economic_depreciation, asset_id=asset.id
            ))
            
        if plan.derogatory_depreciation > 0:
            db.add(AccountingEntry(
                date=date(fiscal_year, 12, 31), account_debit="68725", account_credit="145",
                label=f"Dot. Derog. {asset.name}", amount=plan.derogatory_depreciation, asset_id=asset.id
            ))
        elif plan.derogatory_depreciation < 0:
            db.add(AccountingEntry(
                date=date(fiscal_year, 12, 31), account_debit="145", account_credit="78725",
                label=f"Reprise Derog. {asset.name}", amount=abs(plan.derogatory_depreciation), asset_id=asset.id
            ))
        
        if asset.disposal_date and asset.disposal_date.year == fiscal_year:
            db.add(AccountingEntry(
                date=asset.disposal_date, account_debit="28" + asset.account_number[1:], account_credit="775",
                label=f"Reprise Amort. {asset.name}", amount=plan.cumulative_economic, asset_id=asset.id
            ))
            db.add(AccountingEntry(
                date=asset.disposal_date, account_debit="675", account_credit=asset.account_number,
                label=f"Sortie {asset.name}", amount=asset.acquisition_value, asset_id=asset.id
            ))
    db.commit()
