from models import Asset, DepreciationEntry, AccountingEntry, FiscalYear, DegressiveRule
from sqlalchemy.orm import Session
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

def get_degressive_rate(duration: int, rules: list) -> float:
    for r in rules:
        if r.min_duration <= duration <= r.max_duration:
            return r.coefficient
    return 1.0

def months_between(d1, d2):
    if d1 > d2: return 0
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1

def build_periods(asset: Asset, all_fys: list, rules: list):
    if not asset.is_amortizable: return []
    base = asset.acquisition_value - (asset.residual_value if asset.residual_value else 0.0)
    dur_acc = asset.duration_accounting
    dur_fisc = asset.duration_fiscal if asset.duration_fiscal > 0 else dur_acc
    if base <= 0 or dur_acc <= 0: return []
    
    periods = []
    
    # 1. Périodes réelles
    for fy in all_fys:
        if not fy.start_date or not fy.end_date: continue
        if fy.end_date < asset.service_date: continue
        # NOUVEAU : Arrêter la lecture des exercices si on a dépassé la date de cession
        if asset.disposal_date and fy.start_date > asset.disposal_date: continue
        
        p_start = max(fy.start_date, asset.service_date)
        p_end = fy.end_date
        if asset.disposal_date and p_start <= asset.disposal_date:
            p_end = min(p_end, asset.disposal_date)
        if p_start <= p_end:
            periods.append({'start': p_start, 'end': p_end})
            
    # 2. Prolongation théorique
    last_end = periods[-1]['end'] if periods else date(asset.service_date.year, asset.service_date.month, 1) - timedelta(days=1)
    total_months_acc = dur_acc * 12
    total_months_fisc = dur_fisc * 12
    max_end_date = asset.service_date + relativedelta(months=max(total_months_acc, total_months_fisc))
    
    # NOUVEAU : Si cession, la fin de vie théorique est la date de cession
    if asset.disposal_date:
        max_end_date = min(max_end_date, asset.disposal_date)
    
    while last_end < max_end_date:
        p_start = last_end + timedelta(days=1)
        p_end = p_start + relativedelta(years=1) - timedelta(days=1)
        if asset.disposal_date and p_start <= asset.disposal_date:
            p_end = min(p_end, asset.disposal_date)
        if p_start <= p_end:
            periods.append({'start': p_start, 'end': p_end})
        last_end = p_end

    # 3. Calcul des dotations avec prorata strict sur les mois restants
    timeline = []
    curr_nbv_econ = base
    curr_nbv_fisc = base
    months_elapsed_econ = 0
    months_elapsed_fisc = 0
    
    for p in periods:
        nb_months = months_between(p['start'], p['end'])
        
        # ÉCONOMIQUE
        econ = 0
        months_left_econ = total_months_acc - months_elapsed_econ
        if months_left_econ > 0:
            actual_months_econ = min(nb_months, months_left_econ)
            if asset.method_accounting == 'linear':
                econ = (base / total_months_acc) * actual_months_econ
            else:
                rate = get_degressive_rate(dur_acc, rules) / dur_acc
                remaining_years = months_left_econ / 12.0
                if remaining_years > 0:
                    lin_rate = 1.0 / remaining_years
                    actual_rate = max(rate, lin_rate)
                    econ = (curr_nbv_econ * actual_rate) * (actual_months_econ / 12.0)
        econ = min(econ, curr_nbv_econ)
        
        # FISCAL
        fisc = 0
        months_left_fisc = total_months_fisc - months_elapsed_fisc
        if months_left_fisc > 0:
            actual_months_fisc = min(nb_months, months_left_fisc)
            if asset.method_fiscal == 'linear':
                fisc = (base / total_months_fisc) * actual_months_fisc
            else: 
                rate = get_degressive_rate(dur_fisc, rules) / dur_fisc
                remaining_years = months_left_fisc / 12.0
                if remaining_years > 0:
                    lin_rate = 1.0 / remaining_years
                    actual_rate = max(rate, lin_rate)
                    fisc = (curr_nbv_fisc * actual_rate) * (actual_months_fisc / 12.0)
        fisc = min(fisc, curr_nbv_fisc)
        
        curr_nbv_econ -= econ
        if curr_nbv_econ < 0.01: curr_nbv_econ = 0
        curr_nbv_fisc -= fisc
        if curr_nbv_fisc < 0.01: curr_nbv_fisc = 0
        
        months_elapsed_econ += actual_months_econ if months_left_econ > 0 else 0
        months_elapsed_fisc += actual_months_fisc if months_left_fisc > 0 else 0
        
        timeline.append({'start': p['start'], 'end': p['end'], 'econ': econ, 'fisc': fisc})
        
    return timeline

def generate_plan(asset: Asset, db: Session, all_fys: list, rules: list):
    db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == asset.id).delete(synchronize_session=False)
    if not asset.is_amortizable:
        db.commit()
        return
        
    timeline = build_periods(asset, all_fys, rules)
    if not timeline:
        db.commit()
        return
        
    cumul_econ = 0.0
    cumul_fisc = 0.0
    
    for fy in all_fys:
        if not fy.start_date or not fy.end_date: continue
        if fy.end_date < asset.service_date: continue
        # NOUVEAU : Arrêter d'enregistrer en base si on a dépassé la cession
        if asset.disposal_date and fy.start_date > asset.disposal_date: continue
        
        econ = 0.0
        fisc = 0.0
        for p_data in timeline:
            if p_data['start'] == max(fy.start_date, asset.service_date) and p_data['end'] <= fy.end_date:
                econ += p_data['econ']
                fisc += p_data['fisc']
                
        cumul_econ += econ
        cumul_fisc += fisc
        
        econ_r = round(econ, 2)
        fisc_r = round(fisc, 2)
        derog = round(fisc_r - econ_r, 2)
        
        db.add(DepreciationEntry(
            asset_id=asset.id, fy_start_date=fy.start_date, fiscal_year=fy.start_date.year,
            economic_depreciation=econ_r, fiscal_depreciation=fisc_r, derogatory_depreciation=derog,
            cumulative_economic=round(cumul_econ, 2), cumulative_fiscal=round(cumul_fisc, 2)
        ))
    db.commit()

def generate_accounting_entries(db: Session, fy: FiscalYear):
    db.query(AccountingEntry).filter(AccountingEntry.date >= fy.start_date, AccountingEntry.date <= fy.end_date).delete()
    assets = db.query(Asset).all()
    for asset in assets:
        if not asset.is_amortizable: continue
        plan = db.query(DepreciationEntry).filter(DepreciationEntry.asset_id == asset.id, DepreciationEntry.fy_start_date == fy.start_date).first()
        if not plan: continue
        
        if plan.economic_depreciation > 0:
            db.add(AccountingEntry(date=fy.end_date, account_debit="6811", account_credit="28" + asset.account_number[1:], label=f"Dot. Econ. {asset.name}", amount=plan.economic_depreciation, asset_id=asset.id))
        if plan.derogatory_depreciation > 0:
            db.add(AccountingEntry(date=fy.end_date, account_debit="68725", account_credit="145", label=f"Dot. Derog. {asset.name}", amount=plan.derogatory_depreciation, asset_id=asset.id))
        elif plan.derogatory_depreciation < 0:
            db.add(AccountingEntry(date=fy.end_date, account_debit="145", account_credit="78725", label=f"Reprise Derog. {asset.name}", amount=abs(plan.derogatory_depreciation), asset_id=asset.id))
        
        if asset.disposal_date and fy.start_date <= asset.disposal_date <= fy.end_date:
            db.add(AccountingEntry(date=asset.disposal_date, account_debit="28" + asset.account_number[1:], account_credit="775", label=f"Reprise Amort. {asset.name}", amount=plan.cumulative_economic, asset_id=asset.id))
            db.add(AccountingEntry(date=asset.disposal_date, account_debit="675", account_credit=asset.account_number, label=f"Sortie {asset.name}", amount=asset.acquisition_value, asset_id=asset.id))
    db.commit()
