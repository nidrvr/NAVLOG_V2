import streamlit as st
import datetime
import os
import json

# Import your core engine functions
from navlog_engine import parse_skyvector_fpl, process_flight_plan
from pdf_stamper import create_overlay, generate_unique_filename
from weather_api import get_forecast_altimeter

st.set_page_config(page_title="Seneca VFR Navlog Generator", page_icon="✈️", layout="wide")
st.title("✈️ Seneca VFR Navlog Generator")

# Initialize session state keys
if 'raw_parsed_route' not in st.session_state:
    st.session_state.raw_parsed_route = []
if 'raw_checkpoints' not in st.session_state:
    st.session_state.raw_checkpoints = []
if 'altimeter_setting' not in st.session_state:
    st.session_state.altimeter_setting = 29.92

# Safely load the POH data for the UI to read
try:
    with open('c172_poh.json', 'r') as f:
        POH_DATA = json.load(f)
        # Extract integer altitudes and sort them for the dropdown
        VALID_ALTITUDES = sorted([int(k) for k in POH_DATA.keys()])
except Exception as e:
    st.error(f"Could not load c172_poh.json: {e}")
    POH_DATA = {}
    VALID_ALTITUDES = [2000, 4000, 6000, 8000, 10000, 12000]

# Default schedule: 12 hours in the future (Zulu)
now_utc = datetime.datetime.now(datetime.timezone.utc)
future_utc = now_utc + datetime.timedelta(hours=12)
default_date = future_utc.date()
default_time_str = future_utc.strftime("%H%M")

# ==========================================
# STEP 1: FILE UPLOAD & SCHEDULE
# ==========================================
st.subheader("1. Flight Plan & Schedule")
col_file, col_time = st.columns([2, 1])

with col_file:
    uploaded_file = st.file_uploader(
        "Drop your SkyVector .fpl file here", 
        type=["fpl"], 
        help="Export the .fpl file from SkyVector and drop it here."
    )
    
with col_time:
    dep_date = st.date_input("Departure Date (Z)", value=default_date)
    dep_time_z = st.text_input("Time of Departure (Z)", value=default_time_str, max_chars=4, help="4-digit Zulu time (e.g., 1430)")

if st.button("Parse File & Fetch Weather Data", type="secondary"):
    if uploaded_file is not None:
        try:
            temp_filename = "uploaded_route.fpl"
            with open(temp_filename, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            my_route, raw_checkpoints = parse_skyvector_fpl(temp_filename)
            st.session_state.raw_parsed_route = my_route
            st.session_state.raw_checkpoints = raw_checkpoints
            
            if my_route:
                dep_airport = my_route[0]['id']
                try:
                    hours = int(dep_time_z[:2])
                    minutes = int(dep_time_z[2:])
                    dep_datetime = datetime.datetime.combine(dep_date, datetime.time(hours, minutes), tzinfo=datetime.timezone.utc)
                    
                    with st.spinner(f"Querying weather models for {dep_airport}..."):
                        forecast_alt = get_forecast_altimeter(dep_airport, dep_datetime)
                        if forecast_alt:
                            st.session_state.altimeter_setting = float(forecast_alt)
                            st.success(f"Flight plan parsed! Altimeter forecast for {dep_airport}: {forecast_alt} inHg")
                        else:
                            st.warning("Could not isolate forecast layer. Defaulting to standard 29.92.")
                except Exception as weather_err:
                    st.warning(f"Weather lookup bypassed: Verify departure time format matches HHMM. ({weather_err})")
            
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

        except Exception as e:
            st.error(f"Error parsing .fpl file: {e}")
    else:
        st.warning("Please upload a .fpl file first.")

st.divider()

if len(st.session_state.raw_parsed_route) >= 2:
    
    # ==========================================
    # STEP 2: FLIGHT DETAILS & FUEL RESERVES
    # ==========================================
    st.subheader("2. Aircraft & Fuel Policies")
    col1, col2, col3 = st.columns(3)
    
    with col1:
        pilot_name = st.text_input("Pilot Name", placeholder="e.g., J. Doe")
        aircraft_id = st.text_input("Aircraft Ident / Type", value="", placeholder="C-FESC")
        altimeter = st.number_input("Altimeter Setting (inHg)", value=st.session_state.altimeter_setting, format="%.2f", step=0.01)
        
    with col2:
        # Intentionally left blank or you can use this space for future inputs
        pass
        
    with col3:
        # Intentionally left blank or you can use this space for future inputs
        pass

    st.divider()

    # ==========================================
    # STEP 3: WAYPOINT ROLE ASSIGNMENT
    # ==========================================
    st.subheader("3. Waypoint Roles")
    
    departure_point = st.session_state.raw_parsed_route[0]
    destination_point = st.session_state.raw_parsed_route[-1]
    
    st.info(f"🛫 **Departure:** {departure_point['id']} &nbsp;&nbsp;|&nbsp;&nbsp; 🛬 **Final Destination:** {destination_point['id']}")
    
    turning_points = []
    destinations = [destination_point['id']]
    user_assigned_checkpoints = []
    final_route = [departure_point]
    
    for idx, point in enumerate(st.session_state.raw_parsed_route[1:-1], start=1):
        point_id = point.get('id', f'Point_{idx}')
        
        role = st.radio(
            f"**{point_id}**",
            options=["Enroute Checkpoint (Page 2)", "Set Heading Point (SHP)", "Intermediate Destination (Full Stop)"],
            horizontal=True,
            key=f"role_assign_{idx}"
        )
        
        if role == "Set Heading Point (SHP)":
            turning_points.append(point_id)
            final_route.append(point)
        elif role == "Intermediate Destination (Full Stop)":
            destinations.append(point_id)
            final_route.append(point)
        else:
            user_assigned_checkpoints.append(point)
            
    final_route.append(destination_point)

    st.divider()

    # ==========================================
    # STEP 4: LEG-BY-LEG CONFIGURATIONS
    # ==========================================
    st.subheader("4. Leg Configurations (Altitude & RPM)")
    st.markdown("Select an altitude to view the available performance profiles from the POH.")

    leg_configs = {}
    max_planned_gph = 8.5 # Fallback baseline

    for i in range(len(final_route) - 1):
        from_pt = final_route[i]['id']
        to_pt = final_route[i+1]['id']
        
        st.markdown(f"**Leg: {from_pt} ➔ {to_pt}**")
        col_alt, col_rpm, col_bhp = st.columns([2, 2, 1])
        
        with col_alt:
            # Swap to number_input to allow custom values like 3500
            alt_val = st.number_input(f"Altitude (ft)", min_value=1000, max_value=14000, step=500, value=2000, key=f"alt_{i}")
            
        alt_str = str(alt_val)
        poh_alts = sorted([int(k) for k in POH_DATA.keys()])
        
        # Find the ceiling altitude in the POH to restrict impossible RPMs at custom altitudes
        ceiling_alt = str(next((a for a in poh_alts if a >= alt_val), max(poh_alts)))
        
        if ceiling_alt in POH_DATA and "ISA" in POH_DATA[ceiling_alt]:
            valid_rpms = sorted([int(rpm) for rpm in POH_DATA[ceiling_alt]["ISA"].keys()], reverse=True)
        else:
            valid_rpms = [2600, 2550, 2500, 2400, 2300, 2200, 2100]
    
        with col_rpm:
            rpm_val = st.selectbox(f"RPM Setting", options=valid_rpms, key=f"rpm_{i}")
            
        with col_bhp:
            rpm_str = str(rpm_val)
            bhp = "N/A"
            if ceiling_alt in POH_DATA and "ISA" in POH_DATA[ceiling_alt]:
                bhp_data = POH_DATA[ceiling_alt]["ISA"].get(rpm_str, {})
                bhp = bhp_data.get("MCP", "N/A")
                
                # Track max GPH for Step 5 fuel reserves
                leg_gph = bhp_data.get("GPH", 0)
                if isinstance(leg_gph, (int, float)) and leg_gph > max_planned_gph:
                    max_planned_gph = float(leg_gph)
            
            st.markdown("<div style='margin-top: 32px;'></div>", unsafe_allow_html=True)
            st.write(f"**BHP: {bhp}%**")
        
    leg_configs[to_pt] = {"altitude": alt_str, "rpm": rpm_str}
    final_route[i+1]['altitude'] = alt_val
    final_route[i+1]['rpm'] = rpm_val

    st.divider()
    # ==========================================
    # STEP 5: GENERATION
    # ==========================================
    st.subheader("5. Fuel Reserves & Compile Flight Log")

    col1, col2, col3 = st.columns(3)

    with col1:
        planned_ramp_fuel = st.number_input("Planned Ramp Fuel (Gallons)", min_value=0.0, value=0.0, step=1.0, help="Leave as 0 to calculate minimum legally required fuel layout.")
    
    with col2:
        st.markdown("**Contingency Rules**")
        cont_time = st.number_input("Contingency Fuel (Minutes)", min_value=0, value=10, step=5)
        cont_flow = st.number_input("Contingency Burn Rate (GPH)", min_value=1.0, value=max_planned_gph, step=0.1)
        
    with col3:
        st.markdown("**Reserve Rules**")
        res_time = st.number_input("Reserve Fuel (Minutes)", min_value=0, value=30, step=5)
        res_flow = st.number_input("Reserve Burn Rate (GPH)", min_value=1.0, value=max_planned_gph, step=0.1)

        if st.button("Generate Official Navlog PDF", type="primary", use_container_width=True):
            with st.spinner("Processing performance math models and stamping PDF..."):
                try:
                    FLIGHT_PROFILES = {
                        "FLIGHT_DATE": dep_date.strftime("%Y-%m-%d"),
                        "FLIGHT_TIME": dep_time_z,
                        "PILOT_NAME": pilot_name,
                        "AIRCRAFT_ID": aircraft_id,
                        "ALTIMETER": altimeter,
                        "TURNING_POINTS": turning_points,
                        "DESTINATIONS": destinations,
                        "FUEL_RESERVES": {
                            "cont_min": cont_time, 
                            "cont_gph": cont_flow, 
                            "omit_reserve": False,
                            "planned_ramp_fuel": planned_ramp_fuel if planned_ramp_fuel > 0 else ""
                        },
                        "FINAL_ROUTE": final_route
                    }
                    
                    # Merge the leg-specific configs into the main dictionary for engine compatibility
                    for to_id, config in leg_configs.items():
                        FLIGHT_PROFILES[to_id] = config
    
                    # 1. Execute performance math on major legs
                    calculated_legs = process_flight_plan(final_route, leg_configs=FLIGHT_PROFILES)
                    
                    # 2. Append all checkpoints
                    from checkpoint_math import process_checkpoints
                    combined_checkpoints = st.session_state.raw_checkpoints + user_assigned_checkpoints
                    
                    processed_checkpoints = process_checkpoints(
                        final_route, 
                        calculated_legs, 
                        combined_checkpoints, 
                        turning_points
                    )
    
                    # 3. Compile fuel layout
                    total_enroute_fuel = sum(float(leg.get('fuel_req', 0)) for leg in calculated_legs)
                    startup_fuel = 1.4
                    calculated_cont_fuel = (cont_time / 60.0) * cont_flow
                    calculated_res_fuel = (res_time / 60.0) * res_flow
                    total_ramp_fuel = startup_fuel + total_enroute_fuel + calculated_cont_fuel + calculated_res_fuel
                    
                    starting_pfob = planned_ramp_fuel if planned_ramp_fuel > 0 else total_ramp_fuel
    
                    # 4. Generate PDF
                    output_filename = generate_unique_filename(final_route)
                    
                    create_overlay(
                        legs=calculated_legs,
                        route_data=final_route,
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
                        st.error("Stamping engine execution succeeded, but output file asset was missing.")
    
                except Exception as e:
                    st.error(f"Execution error inside navlog calculation script: {str(e)}")
