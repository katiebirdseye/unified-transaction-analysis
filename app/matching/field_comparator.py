from __future__ import annotations

import re
from typing import Iterable, Optional


class FieldComparator:
    """
    Utility for cleaning, normalizing, and deduplicating values collected
    from AIQ, Archive, and container logs.

    Despite the existing class name, this version does not calculate
    MATCH, MISMATCH, or MISSED results.

    Raw values should normally remain unchanged in the final output.
    Normalization is available for internal grouping, duplicate detection,
    and future comparison features.
    """

    EMPTY_VALUES = {
        "",
        "/",
        "-",
        "NONE",
        "NULL",
        "N/A",
        "NA",
        "UNKNOWN",
    }

    CHARACTER_SUBSTITUTIONS = {
        "O": "0",
        "I": "1",
        "L": "1",
        "S": "5",
        "B": "8",
        "Z": "2",
        "G": "6",
    }

    VEHICLE_TYPE_ALIASES = {
        "TRACTORTRAILER": "TRACTOR_TRAILER",
        "TRACTORWITHTRAILER": "TRACTOR_TRAILER",
        "TRACTORANDTRAILER": "TRACTOR_TRAILER",
        "TRUCKTRAILER": "TRACTOR_TRAILER",
        "SEMITRUCK": "TRACTOR_TRAILER",
        "SEMITRAILER": "TRACTOR_TRAILER",

        "TRACTOR": "TRACTOR",
        "TRACTORONLY": "TRACTOR",
        "POWERUNIT": "TRACTOR",
        "BOBTAIL": "TRACTOR",

        "STRAIGHTTRUCK": "STRAIGHT_TRUCK",
        "BOXTRUCK": "STRAIGHT_TRUCK",
        "CUBEVAN": "STRAIGHT_TRUCK",

        "CAR": "PASSENGER_VEHICLE",
        "SUV": "PASSENGER_VEHICLE",
        "VAN": "PASSENGER_VEHICLE",
        "PICKUP": "PASSENGER_VEHICLE",
        "PICKUPTRUCK": "PASSENGER_VEHICLE",

        "CONTAINER": "CONTAINER",
        "CHASSIS": "CHASSIS",
    }

    # ------------------------------------------------------------------
    # Raw-value handling
    # ------------------------------------------------------------------

    def clean_raw_value(
        self,
        value: object,
    ) -> Optional[str]:
        """
        Return a cleaned display value without changing its content.

        Examples:
            "  TRK123  " -> "TRK123"
            "unknown"    -> None
            ""           -> None
            None         -> None
        """

        if value is None:
            return None

        cleaned = str(value).strip()

        if not cleaned:
            return None

        if cleaned.upper() in self.EMPTY_VALUES:
            return None

        return cleaned

    def add_unique_raw_value(
        self,
        values: list[str],
        value: object,
        *,
        normalized_deduplication: bool = False,
    ) -> None:
        """
        Add a value to a list while preserving first-seen order.

        By default, only exact cleaned duplicates are removed.

        Example:
            TRK123
            TRK
            123

        All three values are retained.

        When normalized_deduplication=True, formatting variants such as
        "TRK-123" and "TRK123" are treated as the same value.
        """

        cleaned = self.clean_raw_value(value)

        if cleaned is None:
            return

        if normalized_deduplication:
            candidate_key = self.normalize_identifier(cleaned)

            existing_keys = {
                self.normalize_identifier(existing)
                for existing in values
            }

            if candidate_key in existing_keys:
                return

        elif cleaned in values:
            return

        values.append(cleaned)

    def ordered_unique_raw_values(
        self,
        values: Iterable[object],
        *,
        normalized_deduplication: bool = False,
    ) -> list[str]:
        """
        Return distinct cleaned values while preserving first-seen order.
        """

        result: list[str] = []

        for value in values:
            self.add_unique_raw_value(
                result,
                value,
                normalized_deduplication=normalized_deduplication,
            )

        return result

    # ------------------------------------------------------------------
    # Identifier normalization
    # ------------------------------------------------------------------

    def normalize_identifier(
        self,
        value: object,
        *,
        apply_character_substitutions: bool = False,
    ) -> str:
        """
        Normalize truck and trailer identifiers for internal use.

        Raw output values should generally not be replaced with this value.

        Examples:
            "TRK-123" -> "TRK123"
            " trk 123 " -> "TRK123"

        When apply_character_substitutions=True:
            "ABO12" -> "AB012"
        """

        cleaned = self.clean_raw_value(value)

        if cleaned is None:
            return ""

        normalized = re.sub(
            r"[^A-Z0-9]",
            "",
            cleaned.upper(),
        )

        if apply_character_substitutions:
            normalized = "".join(
                self.CHARACTER_SUBSTITUTIONS.get(
                    character,
                    character,
                )
                for character in normalized
            )

        return normalized

    # ------------------------------------------------------------------
    # Vehicle-type normalization
    # ------------------------------------------------------------------

    def normalize_vehicle_type(
        self,
        value: object,
    ) -> str:
        """
        Normalize source-specific vehicle-type labels into a canonical value.

        This is intended for internal grouping and future analysis.
        The original raw vehicle-type value should still be retained.
        """

        cleaned = self.clean_raw_value(value)

        if cleaned is None:
            return ""

        compact = re.sub(
            r"[^A-Z0-9]",
            "",
            cleaned.upper(),
        )

        return self.VEHICLE_TYPE_ALIASES.get(
            compact,
            compact,
        )

    def ordered_unique_vehicle_types(
        self,
        values: Iterable[object],
        *,
        normalize_aliases: bool = False,
    ) -> list[str]:
        """
        Return unique vehicle-type values in first-seen order.

        When normalize_aliases=False:
            "tractor trailer" and "truck trailer" remain separate.

        When normalize_aliases=True:
            aliases that map to the same canonical vehicle type are
            deduplicated, while the first raw value is preserved.
        """

        result: list[str] = []
        seen_keys: set[str] = set()

        for value in values:
            cleaned = self.clean_raw_value(value)

            if cleaned is None:
                continue

            key = (
                self.normalize_vehicle_type(cleaned)
                if normalize_aliases
                else cleaned
            )

            if key in seen_keys:
                continue

            seen_keys.add(key)
            result.append(cleaned)

        return result

    # ------------------------------------------------------------------
    # Export formatting
    # ------------------------------------------------------------------

    def format_values_for_export(
        self,
        values: Iterable[object],
        *,
        separator: str = ", ",
    ) -> str:
        """
        Convert a list of values into a spreadsheet-friendly string.

        Example:
            ["TRK123", "TRK", "123"]
            -> "TRK123, TRK, 123"
        """

        cleaned_values = self.ordered_unique_raw_values(values)

        return separator.join(cleaned_values)