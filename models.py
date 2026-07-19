from sqlalchemy import create_engine, Column, String, Integer, Float, Date, Text, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, relationship
import datetime

Base = declarative_base()

class CompanyInfo(Base):
    __tablename__ = 'company_info'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    siret = Column(String)
    address = Column(Text)
    legal_form = Column(String)
    current_fiscal_year_id = Column(Integer, ForeignKey('fiscal_years.id'))

class FiscalYear(Base):
    __tablename__ = 'fiscal_years'
    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False, unique=True)
    is_closed = Column(Boolean, default=False)

class Account(Base):
    __tablename__ = 'accounts'
    account_number = Column(String, primary_key=True)
    label = Column(String, nullable=False)
    account_type = Column(String)

class Category(Base):
    __tablename__ = 'categories'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)

class CostCenter(Base):
    __tablename__ = 'cost_centers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)

class Asset(Base):
    __tablename__ = 'assets'
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    account_number = Column(String, ForeignKey('accounts.account_number'), nullable=False)
    cost_center_id = Column(Integer, ForeignKey('cost_centers.id'), nullable=True)
    location = Column(String)
    supplier = Column(String)
    acquisition_date = Column(Date, nullable=False)
    service_date = Column(Date, nullable=False)
    disposal_date = Column(Date, nullable=True)
    disposal_price = Column(Float, default=0.0)
    acquisition_value = Column(Float, nullable=False)
    residual_value = Column(Float, default=0.0)
    is_amortizable = Column(Boolean, default=True)
    duration_accounting = Column(Integer, nullable=False, default=0)
    duration_fiscal = Column(Integer, nullable=False, default=0)
    method_fiscal = Column(String, default='linear')
    status = Column(String, default='in_service')
    depreciation_entries = relationship("DepreciationEntry", back_populates="asset", cascade="all, delete-orphan")

class DepreciationEntry(Base):
    __tablename__ = 'depreciation_entries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(String, ForeignKey('assets.id'), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    economic_depreciation = Column(Float, default=0.0)
    fiscal_depreciation = Column(Float, default=0.0)
    derogatory_depreciation = Column(Float, default=0.0)
    cumulative_economic = Column(Float, default=0.0)
    cumulative_fiscal = Column(Float, default=0.0)
    asset = relationship("Asset", back_populates="depreciation_entries")

class AccountingEntry(Base):
    __tablename__ = 'accounting_entries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    account_debit = Column(String, nullable=False)
    account_credit = Column(String, nullable=False)
    label = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    asset_id = Column(String, ForeignKey('assets.id'), nullable=True)
