import requests
import datetime
import airportsdata

# Load this globally so it only processes once
AIRPORT_DB = airportsdata.load('icao')


def get_live_metar_data(station_id):
    """
    Fetches live altimeter setting (inHg) and surface temperature (C)
    using the global AWC JSON API.
    """
    url = f"https://aviationweather.gov/api/data/metar?ids={station_id}&format=json"

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data:
            altimeter = data[0].get('altim')
            temp = data[0].get('temp')
            return altimeter, temp

    except requests.exceptions.RequestException as e:
        print(f"[!] Failed to fetch METAR for {station_id}: {e}")

    return None, None


def get_forecast_altimeter(station_id, departure_zulu):
    """
    Fetches the forecasted altimeter setting (inHg) for the planned departure hour
    using the global ECMWF model via Open-Meteo.
    """
    station_id = station_id.strip().upper()
    if station_id not in AIRPORT_DB:
        print(f"   [!] Airport {station_id} not found in database. Using standard 29.92.")
        return 29.92

    lat = AIRPORT_DB[station_id]['lat']
    lon = AIRPORT_DB[station_id]['lon']

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"hourly=pressure_msl&models=ecmwf_ifs025&forecast_days=2"
    )

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        target_hour_str = departure_zulu.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        times = data["hourly"]["time"]

        idx = times.index(target_hour_str) if target_hour_str in times else 0

        hpa = data["hourly"]["pressure_msl"][idx]
        inHg = hpa * 0.0295300

        return round(inHg, 2)

    except Exception as e:
        print(f"   [!] Failed to fetch altimeter forecast: {e}")
        return 29.92


def get_live_upper_winds(station_id, altitude, lat=None, lon=None, target_zulu=None):
    """
    Fetches HRDPS (micro) and ECMWF (macro) upper winds simultaneously.
    Flags a warning if the local HRDPS model heavily deviates from the macro ECMWF airmass.
    """
    station_id = station_id.strip().upper()

    if lat is None or lon is None:
        if station_id in AIRPORT_DB:
            lat = AIRPORT_DB[station_id]['lat']
            lon = AIRPORT_DB[station_id]['lon']
        else:
            print(f"   [!] Waypoint {station_id} not in database and no coordinates provided. Using zeroes.")
            return {"dir": 0, "spd": 0, "temp": 0, "variance_warning": False}

    pressure_levels = {
        1000: 360, 975: 1070, 950: 1800, 925: 2500, 900: 3250,
        850: 4800, 800: 6400, 700: 9900, 600: 13800, 500: 18300
    }

    best_hpa = min(pressure_levels.keys(), key=lambda p: abs(pressure_levels[p] - altitude))
    level = f"{best_hpa}hPa"

    # Requesting both HRDPS and ECMWF models in a single call
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"hourly=wind_speed_{level},wind_direction_{level},temperature_{level}&"
        f"wind_speed_unit=kn&models=gem_hrdps_continental,ecmwf_ifs025&forecast_days=2"
    )

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if target_zulu:
            target_hour_str = target_zulu.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        else:
            target_hour_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:00")

        times = data["hourly"]["time"]
        base_idx = times.index(target_hour_str) if target_hour_str in times else 0

        # Extract HRDPS Data
        hrdps_dir = data["hourly"].get(f"wind_direction_{level}_gem_hrdps_continental", [None])[base_idx]
        hrdps_spd = data["hourly"].get(f"wind_speed_{level}_gem_hrdps_continental", [None])[base_idx]
        hrdps_temp = data["hourly"].get(f"temperature_{level}_gem_hrdps_continental", [None])[base_idx]

        # Extract ECMWF Data
        ecmwf_dir = data["hourly"].get(f"wind_direction_{level}_ecmwf_ifs025", [None])[base_idx]
        ecmwf_spd = data["hourly"].get(f"wind_speed_{level}_ecmwf_ifs025", [None])[base_idx]

        # Fallbacks if a model fails to populate
        h_dir = int(hrdps_dir) if hrdps_dir is not None else 0
        h_spd = int(hrdps_spd) if hrdps_spd is not None else 0
        h_tmp = int(hrdps_temp) if hrdps_temp is not None else 0

        e_dir = int(ecmwf_dir) if ecmwf_dir is not None else h_dir
        e_spd = int(ecmwf_spd) if ecmwf_spd is not None else h_spd

        # Calculate circular difference for direction (handles 350° vs 010° as a 20° diff, not 340°)
        dir_diff = abs((h_dir - e_dir + 180) % 360 - 180)
        spd_diff = abs(h_spd - e_spd)

        # Trigger warning if variance exceeds your threshold (10 degrees OR 5 knots)
        variance_warning = (dir_diff > 10) or (spd_diff > 5)

        return {
            "dir": h_dir,
            "spd": h_spd,
            "temp": h_tmp,
            "ecmwf_dir": e_dir,
            "ecmwf_spd": e_spd,
            "variance_warning": variance_warning
        }

    except Exception as e:
        print(f"   [!] Dual-Model lookup failed for {station_id}: {e}")
        return {"dir": 0, "spd": 0, "temp": 0, "variance_warning": False}