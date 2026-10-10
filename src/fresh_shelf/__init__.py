"""fresh_shelf — fresh-department waste vs revenue optimisation.

A small but complete data platform: simulated retail source systems land dirty
files; a Python loader makes them trustworthy in DuckDB; dbt builds a star
schema, a stock ledger and a FEFO depletion model; a Python decision layer
forecasts demand, prices markdowns and sizes replenishment; and a static report
states the dollars.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
