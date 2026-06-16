import streamlit as st
import datetime
import os

# Import your core engine functions
from navlog_engine import parse_skyvector_fpl, process_flight_plan
from pdf_stamper import create_overlay, generate_unique_filename

st.set_page_config(page_title="VFR Navlog Generator", page_icon="✈️", layout="wide")
st.title("✈️ VFR Navlog Generator")
st.markdown("Enter your flight parameters, paste your SkyVector route, and categorize your waypoints.")
st.divider()

# ==========================================
# 1. FLIGHT PARAMETERS
# ==========================================
st.subheader("1. Flight Information")
col1, col2, col3 = st.columns(3)

with col1:
    flight_date = st.date_input("Departure Date", datetime.date.today())
    flight_time = st.time_input("Departure Time", datetime.datetime.now().time())
with col2:
    pilot_name = st.text_input("Pilot Name", placeholder="e.g., J. Doe")
    aircraft_id = st.text_input("Aircraft Ident / Type", value="C172S")
with col3:
    altimeter = st.number_input("Altimeter Setting (inHg)", value=29.92, format="%.2f", step=0.01)

# ==========================================
# 2. ROUTE & DYNAMIC WAYPOINT SELECTOR
# ==========================================
st.subheader("2. Route Configuration")
route_string = st.text_area("Paste SkyVector Route String", help="Paste the text from your SkyVector FPL.")

# We use session_state to remember the route points after we parse them
if 'parsed_route' not in st.session_state:
    st.session_state.parsed_route = []
    st.session_state.raw_checkpoints = []

if st.button("Parse SkyVector Route"):
    if route_string:
        try:
            # You may need to adapt parse_skyvector_fpl to accept a string instead of a file
            my_route, raw_checkpoints = parse_skyvector_fpl(route_string)
            st.session_state.parsed_route = my_route
            st.session_state.raw_checkpoints = raw_checkpoints
            st.success(f"Successfully parsed {len(my_route)} waypoints!")
        except Exception as e:
            st.error(f"Error parsing route: {e}")
    else:
        st.warning("Please paste a route first.")

# ==========================================
# 3. CATEGORIZE POINTS
# ==========================================
point_categories = {}

if st.session_state.parsed_route:
    st.markdown("#### Categorize your parsed points:")

    # Create a nice layout for the radio buttons
    for idx, point in enumerate(st.session_state.parsed_route):
        point_id = point.get('id', f'Point_{idx}')

        # Display each point with a radio button to select its type
        category = st.radio(
            f"**{point_id}**",
            options=["Checkpoint", "SHP / Turning Point", "Destination (Full Stop)"],
            horizontal=True,
            key=f"cat_{idx}"
        )
        point_categories[point_id] = category

    st.divider()

    # ==========================================
    # 4. GENERATION
    # ==========================================
    st.subheader("3. Generate Navigation Log")
    planned_ramp_fuel = st.number_input("Planned Ramp Fuel (Gallons)", min_value=0.0, value=0.0, step=1.0,
                                        help="Leave as 0 to use minimum required calculated fuel.")

    if st.button("Generate Official Navlog PDF", type="primary", use_container_width=True):
        with st.spinner("Calculating flight legs and generating PDF..."):
            try:
                # Build the configuration dictionary manually based on the user's web inputs
                # This entirely replaces your terminal-based get_leg_configs_interactively()

                turning_points = [pid for pid, cat in point_categories.items() if cat == "SHP / Turning Point"]
                destinations = [pid for pid, cat in point_categories.items() if cat == "Destination (Full Stop)"]

                FLIGHT_PROFILES = {
                    "FLIGHT_DATE": flight_date.strftime("%Y-%m-%d"),
                    "FLIGHT_TIME": flight_time.strftime("%H:%M"),
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
                    "FINAL_ROUTE": st.session_state.parsed_route  # Assuming your engine uses this
                }

                # 1. Process calculations
                calculated_legs = process_flight_plan(st.session_state.parsed_route, leg_configs=FLIGHT_PROFILES)

                # 2. Process Checkpoints (import this if needed)
                from checkpoint_math import process_checkpoints

                processed_checkpoints = process_checkpoints(
                    st.session_state.parsed_route,
                    calculated_legs,
                    st.session_state.raw_checkpoints,
                    turning_points
                )

                # 3. Calculate Fuel for the Stamper
                total_enroute_fuel = sum(float(leg.get('fuel_req', 0)) for leg in calculated_legs)
                startup_fuel = 1.4  # From your c172_climb.json logic
                cont_fuel = (10 / 60.0) * 8.5
                res_fuel = (30 / 60.0) * 8.5
                total_ramp_fuel = startup_fuel + total_enroute_fuel + cont_fuel + res_fuel

                starting_pfob = planned_ramp_fuel if planned_ramp_fuel > 0 else total_ramp_fuel

                # 4. Generate the PDF
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

                # 5. Provide the Download Button
                if os.path.exists(output_filename):
                    st.success("Navlog generated successfully!")
                    with open(output_filename, "rb") as pdf_file:
                        st.download_button(
                            label="📥 Download Stamped Navlog PDF",
                            data=pdf_file,
                            file_name=output_filename,
                            mime="application/pdf",
                            use_container_width=True
                        )
                else:
                    st.error("Engine finished, but the output PDF file could not be found.")

            except Exception as e:
                st.error(f"An error occurred during generation: {str(e)}")