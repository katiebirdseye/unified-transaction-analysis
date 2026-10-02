from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TransactionRecord:
    """
    Represents one transaction row loaded from an Archive worksheet.
    """

    mmddyy: Optional[str] = None
    hhmmss: Optional[str] = None
    enters_exits: Optional[str] = None
    drivers_name: Optional[str] = None
    company_name: Optional[str] = None
    vehicle_information: Optional[str] = None
    unit_number: Optional[str] = None
    chassis_number: Optional[str] = None
    trailer: Optional[list[str]] = None
    additional_info: Optional[str] = None
