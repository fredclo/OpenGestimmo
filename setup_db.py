from models import Base, Account, FiscalYear, Category, CostCenter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import datetime, os

DB_FILE = "immobilisations.db"

def init_db():
    if os.path.exists(DB_FILE): 
        print(f"La base {DB_FILE} existe deja.")
        return
    engine = create_engine(f"sqlite:///{DB_FILE}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(FiscalYear(year=datetime.date.today().year, is_closed=False))
    db.add(Category(name="Informatique"))
    db.add(CostCenter(name="Siege"))
    pcg = [
        ("201000", "Frais d'etablissement", "asset"), ("205000", "Concessions, brevets, licences", "asset"),
        ("206000", "Droit au bail", "asset"), ("207000", "Fonds commercial", "asset"),
        ("211000", "Terrains", "asset"), ("212000", "Agencements de terrains", "asset"),
        ("213000", "Constructions", "asset"), ("215000", "Installations techniques", "asset"),
        ("215400", "Materiel industriel", "asset"), ("218000", "Autres immo corporelles", "asset"),
        ("218300", "Materiel bureau et info", "asset"), ("218400", "Mobilier", "asset"),
        ("231000", "Immo corporelles en cours", "asset"),
        ("280500", "Amort. Concessions, brevets", "depreciation"), ("281000", "Amort. Terrains", "depreciation"),
        ("281300", "Amort. Constructions", "depreciation"), ("281500", "Amort. Installations tech.", "depreciation"),
        ("281540", "Amort. Materiel industriel", "depreciation"), ("281800", "Amort. Autres immo corp.", "depreciation"),
        ("281830", "Amort. Materiel bureau", "depreciation"), ("281840", "Amort. Mobilier", "depreciation"),
        ("290000", "Depreciations immo incorp.", "depreciation"), ("291000", "Depreciations immo corp.", "depreciation"),
        ("145000", "Amortissements derogatoires", "special"),
        ("687250", "Dot. amortis. reglementes (derog)", "special"),
        ("787250", "Reprises amortis. reglementes (derog)", "special"),
        ("675000", "Valeurs comptables des elements cedes", "special"),
        ("775000", "Produits des cessions d'elements cedes", "special")
    ]
    for acc in pcg:
        db.add(Account(account_number=acc[0], label=acc[1], account_type=acc[2]))
    db.commit()
    print(f"Base de donnees {DB_FILE} creee avec PCG.")

if __name__ == "__main__": init_db()
