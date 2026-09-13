"""V5 service layer (P0A).

Modules in this package are the only places that persist V5 domain state and
write the matching audit event inside one transaction.  They are deliberately
free of Streamlit imports so the same commands can be called from tests, a
script or a page.
"""
