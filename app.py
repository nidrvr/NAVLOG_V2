import streamlit as st
import datetime
import os

# Import your core engine functions
from navlog_engine import parse_skyvector_fpl, process_flight_plan
from pdf_stamper import create_overlay, generate_unique_filename
from weather_api import get_forecast_altimeter  # Your HRDPS lookup library

st.set_page_config(page_title="VFR Navlog Generator", page_icon="✈️", layout="wide")
st.title("✈️ VFR Navlog Generator")

# Initialize session state keys to preserve data between dynamic screen re-runs
if 'parsed_route' not in st.session_state:
    st.session_state.parsed_route = []
if 'raw_checkpoints' not in st.session_state:
    st.session_state.raw_checkpoints = []
if 'altimeter_setting' not in st.session_state:
    st.session_state.altimeter_setting = 29.92  # Default fallback standard

# Calculate default schedule: exactly 12 hours in the future (Zulu/UTC)
now_utc = datetime.datetime.now(datetime.timezone.utc)
future_utc = now_utc + datetime.timedelta(hours=12)
default_date = future_utc.date()
default_time_str = future_utc.strftime("%H%M")  # Formats as 4-digit string, e.g., "0330"

# ==========================================
# STEP 1: FILE UPLOAD & SCHEDULE (FIRST)
# ==========================================
st.subheader("1. Flight Plan & Schedule")

# Replaced text box with a clean drag-and-drop file uploader zone
uploaded_file = st.file_uploader("Drop your SkyVector .fpl file here", type=["fpl"], help="Export the .fpl file from SkyVector and drop it here.")

col_time1, col_time2 = st.columns(2)
with col_time1:
    dep_date = st.date_input("Departure Date (Z)", value=default_date)
with col_time2:
    dep_time_z = st.text_input("Time of Departure (Z)", value=default_time_str, max_chars=4, help="Enter a 4-digit Zulu time (e.g., 1430)")

# Parses flight track directly from the uploaded file data and triggers weather lookup
if st.button("Parse File & Fetch Weather Data", type="secondary"):
    if uploaded_file is not None:
        try:
            # Read the raw XML/text content directly from the uploaded file asset
            fpl_content = uploaded_file.getvalue().decode("utf-8")
            
            # Pass the file content text directly to your existing parser
            my_route, raw_checkpoints = parse_skyvector_fpl(fpl_content)
            st.session_state.parsed_route = my_route
            st.session_state.raw_checkpoints = raw_checkpoints
            
            # Extract departure fix and query HRDPS model automatically
            if my_route:
                dep_airport = my_route[0]['id']
                
                try:
                    # Convert text entries back to datetime object for the API
                    hours = int(dep_time_z[:2])
                    minutes = int(dep_time_z[2:])
                    dep_datetime = datetime.datetime.combine(dep_date, datetime.time(hours, minutes), tzinfo=datetime.timezone.utc)
                    
                    with st.spinner(f"Querying HRDPS forecast models for {dep_airport}..."):
                        forecast_alt = get_forecast_altimeter(dep_airport, dep_datetime)
                        if forecast_alt:
                            st.session_state.altimeter_setting = float(forecast_alt)
                            st.success(f"Flight plan parsed successfully! HRDPS Altimeter forecast for {dep_airport}: {forecast_alt} inHg")
                        else:
                            st.warning("Could not isolate HRDPS forecast layer. Defaulting to standard 29.92.")
                except Exception as weather_err:
                    st.warning(f"Weather lookup bypassed: Verify your departure time format matches HHMM. (Error: {weather_err})")
            
        except Exception as e:
            st.error(f"Error parsing .fpl file: {e}")
    else:
        st.warning("Please upload a .fpl file first.")

st.divider()

# The remaining configurations reveal themselves dynamically only after a file is parsed
if st.session_state.parsed_route:
    
    # ==========================================
    # STEP 2: FLIGHT & AIRCRAFT DETAILS
    # ==========================================
    st.subheader("2. Flight & Aircraft Details")
    col1, col2, col3 = st.columns(3)
    
    with col1:
        pilot_name = st.text_input("Pilot Name", placeholder="e.g., J. Doe")
    with col2:
        aircraft_id = st.text_input("Aircraft Ident / Type", value="C172S")
    with col3:
        # Automatically populated by the HRDPS lookup step, but fully adjustable
        altimeter = st.number_input(
            "Altimeter Setting (inHg)", 
            value=st.session_state.altimeter_setting, 
            format="%.2f", 
            step=0.01,
            help="Pre-filled using HRDPS weather forecasts. Adjust this value manually to overrule."
        )

    # ==========================================
    # STEP 3: WAYPOINT ROLE SELECTOR
    # ==========================================
    st.subheader("3. Waypoint Role Assignment")
    point_categories = {}
    
    st.markdown("Select the structural role for each waypoint along your parsed track:")
    for idx, point in enumerate(st.session_state.parsed_route):
        point_id = point.get('id', f'Point_{idx}')
        category = st.radio(
            f"**{point_id}**", 
            options=["Checkpoint", "SHP / Turning Point", "Destination (Full Stop)"],
            horizontal=True,
            key=f"cat_{idx}"
        )
        point_categories[point_id] = category

    st.divider()

    # ==========================================
    # STEP 4: GENERATION
    # ==========================================
    st.subheader("4. Compile Flight Log")
    planned_ramp_fuel = st.number_input("Planned Ramp Fuel (Gallons)", min_value=0.0, value=0.0, step=1.0, help="Leave as 0 to calculate minimum legally required fuel layout.")

    if st.button("Generate Official Navlog PDF", type="primary", use_container_width=True):
        with st.spinner("Processing leg logs and drawing PDF layers..."):
            try:
                turning_points = [pid for pid, cat in point_categories.items() if cat == "SHP / Turning Point"]
                destinations = [pid for pid, cat in point_categories.items() if cat == "Destination (Full Stop)"]
                
                FLIGHT_PROFILES = {
                    "FLIGHT_DATE": dep_date.strftime("%Y-%m-%d"),
                    "FLIGHT_TIME": dep_time_z,
                    "PILOT_NAME": pilot_name,
                    "AIRCRAFT_ID": aircraft_id,
                    "ALTIMETER": altimeter,
                    "TURNING_POINTS": turning_points,
                    "DESTINATIONS": destinations,
                    "FUEL_RESERVES": {
                        "cont_min": 10, 
                        "cont_gph": 8.5, 
                        "omit_reserve": False,
                        "planned_ramp_fuel": planned_ramp_fuel if planned_ramp_fuel > 0 else ""
                    },
                    "FINAL_ROUTE": st.session_state.parsed_route
                }

                # 1. Execute performance calculations
                calculated_legs = process_flight_plan(st.session_state.parsed_route, leg_configs=FLIGHT_PROFILES)
                
                # 2. Append chronological checkpoints
                from checkpoint_math import process_checkpoints
                processed_checkpoints = process_checkpoints(
                    st.session_state.parsed_route, 
                    calculated_legs, 
                    st.session_state.raw_checkpoints, 
                    turning_points
                )

                # 3. Compile fuel layout data
                total_enroute_fuel = sum(float(leg.get('fuel_req', 0)) for leg in calculated_legs)
                startup_fuel = 1.4
                cont_fuel = (10 / 60.0) * 8.5
                res_fuel = (30 / 60.0) * 8.5
                total_ramp_fuel = startup_fuel + total_enroute_fuel + cont_fuel + res_fuel
                
                starting_pfob = planned_ramp_fuel if planned_ramp_fuel > 0 else total_ramp_fuel

                # 4. Generate unique name and draw PDF layout layers
                output_filename = generate_unique_filename(st.session_state.parsed_route)
                
                create_overlay(
                    legs=calculated_legs,
                    route_data=st.session_state.parsed_route,
                    processed_checkpoints=processed_checkpoints,
                    flight_configs=FLIGHT_PROFILES,
                    total_ramp_fuel=total_ramp_fuel,
                    starting_pfob=starting_pfob,
                    output_filename=output_filename
                )

                if os.path.exists(output_filename):
                    st.success("Navlog compiled successfully!")
                    with open(output_filename, "rb") as pdf_file:
                        st.download_button(
                            label="📥 Download Stamped Navlog PDF",
                            data=pdf_file,
                            file_name=output_filename,
                            mime="application/pdf",
                            use_container_width=True
                        )
                else:
                    st.error("Stamping engine complete, but the output file asset could not be verified.")

            except Exception as e:
                st.error(f"Execution error inside navlog script: {str(e)}")
