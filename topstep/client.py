from dotenv import load_dotenv
import os
import time
import requests
from datetime import datetime, timedelta, timezone

load_dotenv() # automatically finds .env file
TOPSTEP_BASE = os.getenv("TOPSTEP_BASE", "https://api.topstepx.com")  
TOPSTEP_USERNAME = os.getenv("TOPSTEP_USERNAME")
TOPSTEP_API_KEY = os.getenv("TOPSTEP_API_KEY")

class TopstepClient:
    def __init__(self):
        if not TOPSTEP_USERNAME or not TOPSTEP_API_KEY:
            raise ValueError("TOPSTEP_USERNAME or TOPSTEP_API_KEY is missing. Check your .env file.")

        # one persistent session per client instance
        self._session = requests.Session()
        self._token = None
        self._token_exp = 0  # epoch timestamp when token expires
        self._base_url = TOPSTEP_BASE

    def login_with_key(self):
        """
        Logs in using a username + apiKey.
        Matches the Postman loginKey endpoint.
        """
        url = f"{TOPSTEP_BASE}/api/Auth/loginKey"

        payload = {
            "userName": TOPSTEP_USERNAME,
            "apiKey": TOPSTEP_API_KEY,
        }

        headers = {
            "accept": "text/plain",
            "Content-Type": "application/json",
        }

        resp = self._session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", False):
            msg = data.get("errorMessage", "Unknown login error")
            raise RuntimeError(f"Topstep login failed: {msg}")

        token = data["token"]      # <-- THIS is the correct token
        self._token = token
        self._token_exp = time.time() + 23 * 3600

        print("Topstep: Login success!")

    def _ensure_token(self):
        now = time.time()

        # 1. No token stored yet
        if not self._token:
            print("Topstep: No token found. Logging in...")
            self.login_with_key()
            self.validate()
            return

        # 2. Token expired
        elif now > self._token_exp:
            print("Topstep: Token expired. Logging in again...")
            self.login_with_key()
            self.validate()
            return

        # 3. Token exists and is not expired
        else:
            # Convert expiration timestamp to readable date/time
            exp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(self._token_exp)
            )

            print(f"Topstep: Token is valid. It will expire at {exp_str}.")

            # Optional: Validate token with the server
            if not self.validate():
                print("Topstep: Server rejected token. Re-authenticating...")
                self.login_with_key()
                self.validate()

    def validate(self) -> bool:
        """
        Validates the current Bearer token and receives a new validated token.
        Returns True if validation succeeded, False otherwise.
        """
        if not self._token:
            print("Topstep: Validate called with no token present.")
            return False

        print("Topstep: Validating token...")

        url = f"{TOPSTEP_BASE}/api/Auth/validate"

        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

        resp = self._session.post(url, headers=headers, timeout=15)
        resp.raise_for_status()

        data = resp.json()

        # If success = false → validation failure
        if not data.get("success", False):
            msg = data.get("errorMessage", "Unknown validation error")
            print(f"Topstep: Token validation failed: {msg}")
            return False

        # Validation success → set new token (Topstep always returns one)
        new_token = data.get("token")
        if new_token:
            self._token = new_token
            self._token_exp = time.time() + 23 * 3600
            print("Topstep: Token validated and refreshed.")
        else:
            print("Topstep: Token valid (no refresh token returned).")

        return True
    
    def get_latest_contract_id(self, search_text: str, live: bool = False) -> str | None:
        """
        Returns ONLY the contract ID string for the active contract.
        Example: 'CON.F.US.MGC.Z25'
        """
        self._ensure_token()

        url = f"{self._base_url}/api/Contract/search"

        payload = {
            "live": live,
            "searchText": search_text
        }

        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        resp = self._session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        contracts = data.get("contracts", [])

        if not contracts:
            print(f"No contracts returned for searchText='{search_text}'.")
            return None

        # They already give only the active contract
        contract_id = contracts[0].get("id")
        print(f"Topstep: Active contract for '{search_text}' → {contract_id}")

        return contract_id


    def get_yesterdays_daily_bar_for_symbol(self, symbol_root: str, live: bool = False):
        """
        Retrieve the most recent CLOSED daily bar for the active contract
        of a given futures symbol root (e.g. 'MGC', 'GC', 'CL').

        Returns:
            A dict like:
            {
                "t": "2025-11-21T00:00:00+00:00",
                "o": 4075.0,
                "h": 4101.1,
                "l": 4019.0,
                "c": 4062.8,
                "v": 455342
            }
            or None if no bars found.
        """
        # 1) Get active contractId for this symbol (e.g. "CON.F.US.MGC.Z25")
        contract_id = self.get_latest_contract_id(symbol_root, live=live)
        if not contract_id:
            print(f"Topstep: Could not resolve active contract for '{symbol_root}'.")
            return None

        # 2) Ensure we have a valid token
        self._ensure_token()

        # 3) Build a date range wide enough to include the last trading day
        end_dt = datetime.now(timezone.utc).replace(microsecond=0)
        start_dt = end_dt - timedelta(days=7)  # wide window, we'll still only take 1 bar

        payload = {
            "contractId": contract_id,
            "live": live,
            "startTime": start_dt.isoformat().replace("+00:00", "Z"),
            "endTime": end_dt.isoformat().replace("+00:00", "Z"),
            "unit": 4,                 # 4 = Day
            "unitNumber": 1,           # 1 day per bar
            "limit": 1,                # only need the latest completed bar
            "includePartialBar": False # excludes today's forming bar
        }

        url = f"{self._base_url}/api/History/retrieveBars"

        headers = {
            "accept": "application/json",   # response body is JSON with "bars": [...]
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        print(f"Topstep: Getting most recent closed daily bar for {symbol_root} ({contract_id})...")
        resp = self._session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        bars = data.get("bars", [])

        if not bars:
            print("Topstep: No daily bars returned.")
            return None

        # Because limit=1, this should just be the most recent completed day
        bar = bars[-1]
        return bar
