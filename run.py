import base64
import getopt
import itertools
import json
import math
import os
import sys

import pandas as pd
import numpy as np
import xlsxwriter
import sklearn.preprocessing as pre
import streamlit as st
import seaborn as sns
import matplotlib.pyplot as plt
import pydeck as pdk
import altair as alt
from six import BytesIO
import geopandas as gpd

import api
import queries
from constants import STATES

# Pandas options
pd.set_option('max_rows', 25)
pd.set_option('max_columns', 12)
pd.set_option('expand_frame_repr', True)
pd.set_option('large_repr', 'truncate')
pd.options.display.float_format = '{:.2f}'.format

HOUSING_STOCK_DISTRIBUTION = {
    # Assumed National housing distribution [https://www.census.gov/programs-surveys/ahs/data/interactive/ahstablecreator.html?s_areas=00000&s_year=2017&s_tablename=TABLE2&s_bygroup1=1&s_bygroup2=1&s_filtergroup1=1&s_filtergroup2=1]
    0: 0.0079,
    1: 0.1083,
    2: 0.2466,
    3: 0.4083,
    4: 0.2289
}

BURDENED_HOUSEHOLD_PROPORTION = [5, 25, 33, 50, 75]
MODE = 'UI'
COLOR_RANGE = [
    [65, 182, 196],
    [127, 205, 187],
    [199, 233, 180],
    [237, 248, 177],
    [255, 255, 204],
    [255, 237, 160],
    [254, 217, 118],
    [254, 178, 76],
    [253, 141, 60],
    [252, 78, 42],
    [227, 26, 28],
]
BREAKS = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1]


def color_scale(val):
    for i, b in enumerate(BREAKS):
        if val < b:
            return COLOR_RANGE[i]
    return COLOR_RANGE[i]


def filter_state(data: pd.DataFrame, state: str) -> pd.DataFrame:
    return data[data['State'].str.lower() == state.lower()]


def filter_counties(data: pd.DataFrame, counties: list) -> pd.DataFrame:
    counties = [_.lower() for _ in counties]
    return data[data['County Name'].str.lower().isin(counties)]


def clean_data(data: pd.DataFrame) -> pd.DataFrame:
    data.set_index(['State', 'County Name'], drop=True, inplace=True)
    data['Non-Home Ownership (%)'] = 100 - pd.to_numeric(data['Home Ownership (%)'], downcast='float')

    data.drop([
        'Home Ownership (%)',
        'Burdened Households Date',
        'Home Ownership Date',
        'Income Inequality Date',
        'Population Below Poverty Line Date',
        'Single Parent Households Date',
        'SNAP Benefits Recipients Date',
        'Unemployment Rate Date',
        'Resident Population Date'
    ], axis=1, inplace=True)
    data = data.loc[:, ~data.columns.str.contains('^Unnamed')]

    return data


def percent_to_population(feature: str, name: str, df: pd.DataFrame) -> pd.DataFrame:
    df[name] = (df[feature].astype(float) / 100) * df['Resident Population (Thousands of Persons)'].astype(float) * 1000
    return df


def cross_features(df: pd.DataFrame) -> pd.DataFrame:
    cols = ['Pop Below Poverty Level', 'Pop Unemployed', 'Income Inequality (Ratio)', 'Non-Home Ownership Pop',
            'Num Burdened Households', 'Num Single Parent Households']
    all_combinations = []
    for r in range(2, 3):
        combinations_list = list(itertools.combinations(cols, r))
        all_combinations += combinations_list
    new_cols = []
    for combo in all_combinations:
        new_cols.append(cross(combo, df))

    crossed_df = pd.DataFrame(new_cols)
    crossed_df = crossed_df.T
    crossed_df['Mean'] = crossed_df.mean(axis=1)

    return crossed_df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = percent_to_population('Population Below Poverty Line (%)', 'Pop Below Poverty Level', df)
    df = percent_to_population('Unemployment Rate (%)', 'Pop Unemployed', df)
    df = percent_to_population('Burdened Households (%)', 'Num Burdened Households', df)
    df = percent_to_population('Single Parent Households (%)', 'Num Single Parent Households', df)
    df = percent_to_population('Non-Home Ownership (%)', 'Non-Home Ownership Pop', df)

    if 'Policy Value' in list(df.columns) or 'Countdown' in list(df.columns):
        df = df.drop(['Policy Value', 'Countdown'], axis=1)

    df = df.drop(['Population Below Poverty Line (%)',
                  'Unemployment Rate (%)',
                  'Burdened Households (%)',
                  'Single Parent Households (%)',
                  'Non-Home Ownership (%)',
                  'Resident Population (Thousands of Persons)',
                  ], axis=1)

    scaler = pre.MaxAbsScaler()
    df_scaled = pd.DataFrame(scaler.fit_transform(df), index=df.index, columns=df.columns)

    return df_scaled


def normalize_column(df: pd.DataFrame, col: str) -> pd.DataFrame:
    scaler = pre.MaxAbsScaler()
    df[col] = scaler.fit_transform(df[col].values.reshape(-1, 1))

    return df


def normalize_percent(percent: float) -> float:
    return percent / 100


def cross(columns: tuple, df: pd.DataFrame) -> pd.Series:
    columns = list(columns)
    new_col = '_X_'.join(columns)
    new_series = pd.Series(df[columns].product(axis=1), name=new_col).abs()
    return new_series


def priority_indicator(socioeconomic_index: float, policy_index: float, time_left: int = 1) -> float:
    if time_left < 1:
        # Handle 0 values
        time_left = 1

    return float(socioeconomic_index) * (1 - float(policy_index)) / math.sqrt(time_left)


def rank_counties(df: pd.DataFrame, label: str) -> pd.DataFrame:
    df.drop(['county_id'], axis=1, inplace=True)
    analysis_df = normalize(df)

    crossed = cross_features(analysis_df)
    analysis_df['Crossed'] = crossed['Mean']
    analysis_df = normalize_column(analysis_df, 'Crossed')

    analysis_df['Relative Risk'] = analysis_df.sum(axis=1)
    max_sum = analysis_df['Relative Risk'].max()
    analysis_df['Relative Risk'] = (analysis_df['Relative Risk'] / max_sum)

    if 'Policy Value' in list(df.columns):
        analysis_df['Policy Value'] = df['Policy Value']
        analysis_df['Countdown'] = df['Countdown']
        analysis_df['Rank'] = analysis_df.apply(
            lambda x: priority_indicator(x['Relative Risk'], x['Policy Value'], x['Countdown']), axis=1
        )

    analysis_df.to_excel('Output/' + label + '_overall_vulnerability.xlsx')

    return analysis_df


def load_all_data() -> pd.DataFrame:
    if os.path.exists("Output/all_tables.xlsx"):
        try:
            res = input('Previous data found. Use data from local `all_tables.xlsx`? [y/N]')
            if res.lower() == 'y' or res.lower() == 'yes':
                df = pd.read_excel('Output/all_tables.xlsx')
            else:
                df = queries.latest_data_all_tables()
        except:
            print('Something went wrong with the Excel file. Falling back to database query.')
            df = queries.latest_data_all_tables()
    else:
        df = queries.latest_data_all_tables()

    return df


def get_existing_policies(df: pd.DataFrame) -> pd.DataFrame:
    policy_df = queries.policy_query()
    temp_df = df.merge(policy_df, on='county_id')
    if not temp_df.empty and len(df) == len(temp_df):
        if MODE == 'SCRIPT':
            res = input('Policy data found in database. Use this data? [Y/n]').strip()
            if res.lower() == 'y' or res.lower() == 'yes' or res == '':
                return temp_df
        elif MODE == 'UI':
            if st.checkbox('Use existing policy data?'):
                return temp_df
    else:
        policy_df = pd.read_excel('Policy Workbook.xlsx', sheet_name='Analysis Data')
        temp_df = df.merge(policy_df, on='County Name')
        if not temp_df.empty and len(df) == len(temp_df):
            return temp_df
        # else:
        #     print(
        #         "INFO: Policy data not found. Check that you've properly filled in the Analysis Data page in `Policy Workbook.xlsx` with the counties you're analyzing.")

    return df


def get_single_county(county: str, state: str) -> pd.DataFrame:
    df = load_all_data()
    df = filter_state(df, state)
    df = filter_counties(df, [county])
    df = get_existing_policies(df)
    df = clean_data(df)

    return df


def get_multiple_counties(counties: list, state: str) -> pd.DataFrame:
    df = load_all_data()
    df = filter_state(df, state)
    df = filter_counties(df, counties)
    df = get_existing_policies(df)
    df = clean_data(df)

    return df


def get_state_data(state: str) -> pd.DataFrame:
    df = load_all_data()
    df = filter_state(df, state)
    df = get_existing_policies(df)
    df = clean_data(df)

    return df


def output_table(df: pd.DataFrame, path: str):
    df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
    df.to_excel(path)


def calculate_cost_estimate(df: pd.DataFrame, pct_burdened: float, distribution: dict,
                            rent_type: str = 'fmr') -> pd.DataFrame:
    if rent_type == 'fmr':
        cost_df = queries.static_data_single_table('fair_market_rents', queries.static_columns['fair_market_rents'])
    elif rent_type == 'rent50':
        cost_df = queries.static_data_single_table('median_rents', queries.static_columns['median_rents'])
        print(cost_df)
    else:
        raise Exception(
            'Invalid input - {x} is not a valid rent type. Must be either `fmr` (Free Market Rent) or `med` (Median Rent)'.format(
                x=rent_type))

    cost_df = cost_df.drop([
        'State',
        'County Name'
    ], axis=1)

    df = df.reset_index().merge(cost_df, how="left", on='county_id').set_index(['State', 'County Name'])
    df = df.astype(float)
    for key, value in distribution.items():
        df['br_cost_0'] = value * df[f'{rent_type}_0'] * (pct_burdened / 100) * (
                df['Resident Population (Thousands of Persons)'] * 1000) * (df['Burdened Households (%)'] / 100)
        df['br_cost_1'] = value * df[f'{rent_type}_1'] * (pct_burdened / 100) * (
                df['Resident Population (Thousands of Persons)'] * 1000) * (df['Burdened Households (%)'] / 100)
        df['br_cost_2'] = value * df[f'{rent_type}_2'] * (pct_burdened / 100) * (
                df['Resident Population (Thousands of Persons)'] * 1000) * (df['Burdened Households (%)'] / 100)
        df['br_cost_3'] = value * df[f'{rent_type}_3'] * (pct_burdened / 100) * (
                df['Resident Population (Thousands of Persons)'] * 1000) * (df['Burdened Households (%)'] / 100)
        df['br_cost_4'] = value * df[f'{rent_type}_4'] * (pct_burdened / 100) * (
                df['Resident Population (Thousands of Persons)'] * 1000) * (df['Burdened Households (%)'] / 100)
        df['total_cost'] = np.sum([df['br_cost_0'], df['br_cost_1'], df['br_cost_2'], df['br_cost_3'], df['br_cost_4']],
                                  axis=0)
    return df


def print_summary(df: pd.DataFrame, output: str):
    print('*** Results ***')
    if 'Rank' in df.columns:
        print('* Shown in order by overall priority, higher values mean higher priority.')
        df.sort_values('Rank', ascending=False, inplace=True)
        print(df['Rank'])
        print('Normalized analysis data is located at {o}'.format(o=output[:-5]) + '_overall_vulnerability.xlsx')
    elif len(df) > 1:
        print('* Shown in order by relative risk, higher values mean higher relative risk.')
        df.sort_values('Relative Risk', ascending=False, inplace=True)
        print(df['Relative Risk'])
        print('Normalized analysis data is located at {o}'.format(o=output[:-5]) + '_overall_vulnerability.xlsx')
    else:
        print('Fetched single county data')

    print('Raw fetched data is located at {o}'.format(o=output))
    print('Done!')


def load_distributions():
    metro_areas = queries.generic_select_query('housing_stock_distribution', [
        'location',
        '0_br_pct',
        '1_br_pct',
        '2_br_pct',
        '3_br_pct',
        '4_br_pct'
    ])
    locations = list(metro_areas['location'])
    metro_areas.set_index('location', inplace=True)

    return metro_areas, locations


def make_map(geo_df: pd.DataFrame, df: pd.DataFrame):
    st.subheader('Map')

    temp = df.copy()
    temp.reset_index(inplace=True)

    counties = temp['County Name'].to_list()

    def convert_coordinates(row):
        for f in row['coordinates']['features']:
            new_coords = []
            if f['geometry']['type'] == 'MultiPolygon':
                f['geometry']['type'] = 'Polygon'
                combined = []
                for i in range(len(f['geometry']['coordinates'])):
                    combined.extend(list(f['geometry']['coordinates'][i]))
                f['geometry']['coordinates'] = combined
            coords = f['geometry']['coordinates']
            for coord in coords:
                for point in coord:
                    new_coords.append([point[0], point[1]])
            f['geometry']['coordinates'] = new_coords
        return row['coordinates']

    def make_geojson(geo_df: pd.DataFrame):
        geojson = {"type": "FeatureCollection", "features": []}
        for i, row in geo_df.iterrows():
            feature = row['coordinates']['features'][0]
            feature["properties"] = {"risk": row['Relative Risk'], "name": row['County Name']}
            del feature["id"]
            del feature["bbox"]
            feature["geometry"]["coordinates"] = [feature["geometry"]["coordinates"]]
            # print(feature)
            geojson["features"].append(feature)

            # if feature["geometry"]["type"] == "MultiPolygon":
            #     print(row['County Name'])

        return geojson

    temp = temp[['County Name', 'Relative Risk']]
    geo_df = geo_df.merge(temp, on='County Name')
    geo_df['geom'] = geo_df.apply(lambda row: row['geom'].buffer(0), axis=1)
    geo_df['coordinates'] = geo_df.apply(lambda row: gpd.GeoSeries(row['geom']).__geo_interface__, axis=1)
    geo_df['coordinates'] = geo_df.apply(lambda row: convert_coordinates(row), axis=1)
    geojson = make_geojson(geo_df)
    json = pd.DataFrame(geojson)
    geo_df["coordinates"] = json["features"].apply(lambda row: row["geometry"]["coordinates"])
    geo_df["name"] = json["features"].apply(lambda row: row["properties"]["name"])
    geo_df["risk"] = json["features"].apply(lambda row: row["properties"]["risk"])
    geo_df["fill_color"] = json["features"].apply(lambda row: color_scale(row["properties"]["risk"]))
    geo_df.drop(['geom', 'County Name', 'Relative Risk'], axis=1, inplace=True)

    view_state = pdk.ViewState(
        **{"latitude": 36, "longitude": -95, "zoom": 3, "maxZoom": 16, "pitch": 0, "bearing": 0}
    )
    polygon_layer = pdk.Layer(
        "PolygonLayer",
        geo_df,
        get_polygon="coordinates",
        filled=True,
        stroked=False,
        opacity=0.5,
        get_fill_color='fill_color',
        auto_highlight=True,
        pickable=True,
    )
    tooltip = {"html": "<b>County:</b> {name} </br> <b>Risk:</b> {risk}"}

    r = pdk.Deck(
        layers=[polygon_layer],
        initial_view_state=view_state,
        map_style=pdk.map_styles.LIGHT,
        tooltip=tooltip
    )
    st.pydeck_chart(r)


def make_correlation_plot(df: pd.DataFrame):
    st.subheader('Correlation Plot')
    fig, ax = plt.subplots(figsize=(10, 10))
    st.write(sns.heatmap(df.corr(), annot=True, linewidths=0.5))
    st.pyplot(fig)


def visualizations(df: pd.DataFrame, state: str = None):
    st.write('## Charts')
    if state:
        temp = df.copy()
        temp.reset_index(inplace=True)
        counties = temp['County Name'].to_list()
        if state != 'national':
            geo_df = queries.get_county_geoms(counties, state.lower())
            make_map(geo_df, df)

    make_correlation_plot(df)


def to_excel(df: pd.DataFrame):
    output = BytesIO()
    writer = pd.ExcelWriter(output, engine='xlsxwriter')
    df.to_excel(writer, sheet_name='Sheet1')
    writer.save()
    processed_data = output.getvalue()
    return processed_data


def get_table_download_link(df: pd.DataFrame, file_name: str, text: str):
    """Generates a link allowing the data in a given panda dataframe to be downloaded
    in:  dataframe
    out: href string
    """
    val = to_excel(df)
    b64 = base64.b64encode(val)  # val looks like b'...'
    return f'<a href="data:application/octet-stream;base64,{b64.decode()}" download="{file_name}.xlsx">{text}</a>'


def data_explorer(df: pd.DataFrame, state: str):
    feature_labels = list(
        set(df.columns) - {'County Name', 'county_id', 'Resident Population (Thousands of Persons)'})
    col1, col2, col3 = st.beta_columns(3)
    with col1:
        feature_1 = st.selectbox('X Feature', feature_labels, 0)
    with col2:
        feature_2 = st.selectbox('Y Feature', feature_labels, 1)
    if feature_1 and feature_2:
        chart_data = df.reset_index()[
            [feature_1, feature_2, 'County Name', 'Resident Population (Thousands of Persons)']]
        c = alt.Chart(chart_data).mark_point().encode(x=feature_1, y=feature_2, tooltip=['County Name',
                                                                                         'Resident Population (Thousands of Persons)',
                                                                                         feature_1, feature_2],
                                                      color='County Name',
                                                      size='Resident Population (Thousands of Persons)')
        st.altair_chart(c, use_container_width=True)

    # visualizations(df, state)


def cost_of_evictions(df, metro_areas, locations):
    rent_type = st.selectbox('Rent Type', ['Fair Market', 'Median'])
    location = st.selectbox('Select a location to assume a housing distribution:', locations)
    distribution = {
        0: float(metro_areas.loc[location, '0_br_pct']),
        1: float(metro_areas.loc[location, '1_br_pct']),
        2: float(metro_areas.loc[location, '2_br_pct']),
        3: float(metro_areas.loc[location, '3_br_pct']),
        4: float(metro_areas.loc[location, '4_br_pct']),
    }

    pct_burdened = st.slider('Percent of Burdened Population to Support', 0, 100, value=50, step=1)

    if rent_type == '' or rent_type == 'Fair Market':
        df = calculate_cost_estimate(df, pct_burdened, rent_type='fmr', distribution=distribution)
    elif rent_type == 'Median':
        df = calculate_cost_estimate(df, pct_burdened, rent_type='rent50', distribution=distribution)

    cost_df = df.reset_index()
    cost_df.drop(columns=['State'], inplace=True)
    cost_df.set_index('County Name', inplace=True)
    # cost_df = cost_df[['br_cost_0', 'br_cost_1', 'br_cost_2', 'br_cost_3', 'br_cost_4', 'total_cost']]
    # st.dataframe(
    #     cost_df[['total_cost']])
    st.bar_chart(cost_df['total_cost'])
    return cost_df


def run_UI():
    st.sidebar.write("""
    # Arup Social Data

    This tool supports analysis of county level data from a variety of data sources.

    More documentation and contribution details are at our [GitHub Repository](https://github.com/arup-group/eviction-data)
    """)
    workflow = st.sidebar.selectbox('Workflow', ['Eviction Analysis', 'Data Explorer'])
    if workflow == 'Eviction Analysis':
        st.write('### Eviction Data Analysis')
        task = st.selectbox('What type of analysis are you doing?',
                            ['Single County', 'Multiple Counties', 'State', 'National'])
        metro_areas, locations = load_distributions()

        if task == 'Single County' or task == '':
            res = st.text_input('Enter the county and state (ie: Jefferson County, Colorado):')
            if res:
                res = res.strip().split(',')
                county = res[0].strip()
                state = res[1].strip()
                if county and state:
                    df = get_single_county(county, state)
                    st.write(df)
                    if st.checkbox('Show raw data'):
                        st.subheader('Raw Data')
                        st.dataframe(df)
                        st.markdown(get_table_download_link(df, county + '_data', 'Download raw data'),
                                    unsafe_allow_html=True)

                    if st.checkbox('Do cost to avoid eviction analysis?'):
                        evictions_cost_df = cost_of_evictions(df, metro_areas, locations)
                        if st.checkbox('Show cost data'):
                            st.dataframe(evictions_cost_df)
                        st.markdown(
                            get_table_download_link(evictions_cost_df, county + '_cost_data', 'Download cost data'),
                            unsafe_allow_html=True)

                else:
                    st.warning('Enter a valid county and state, separated by a comma')
                    st.stop()

        elif task == 'Multiple Counties':
            state = st.selectbox("Select a state", STATES).strip()
            county_list = queries.counties_query()
            county_list = county_list[county_list['State'] == state]['County Name'].to_list()
            counties = st.multiselect('Please specify one or more counties', county_list)
            counties = [_.strip().lower() for _ in counties]
            if len(counties) > 0:
                df = get_multiple_counties(counties, state)

                if st.checkbox('Show raw data'):
                    st.subheader('Raw Data')
                    st.dataframe(df)
                    st.markdown(get_table_download_link(df, state + '_custom_data', 'Download raw data'),
                                unsafe_allow_html=True)

                if st.checkbox('Do cost to avoid eviction analysis?'):
                    evictions_cost_df = cost_of_evictions(df, metro_areas, locations)
                    if st.checkbox('Show cost data'):
                        st.dataframe(evictions_cost_df)
                    st.markdown(
                        get_table_download_link(evictions_cost_df, state + '_custom_cost_data', 'Download cost data'),
                        unsafe_allow_html=True)

                # output_table(df, 'Output/' + state + '_selected_counties.xlsx')
                # st.success('Data was saved at `' + 'Output/' + state + '_selected_counties.xlsx')
                ranks = rank_counties(df, state + '_selected_counties').sort_values(by='Relative Risk', ascending=False)
                st.write('## Results')
                st.dataframe(ranks)
                st.markdown(get_table_download_link(ranks, state + '_custom_ranking', 'Download Relative Risk ranking'),
                            unsafe_allow_html=True)

                visualizations(ranks, state)
            else:
                st.warning('Select counties to analyze')
                st.stop()
        elif task == 'State':
            state = st.selectbox("Select a state", STATES).strip()
            df = get_state_data(state)

            if st.checkbox('Show raw data'):
                st.subheader('Raw Data')
                st.dataframe(df)
                st.markdown(get_table_download_link(df, state + '_data', 'Download raw data'), unsafe_allow_html=True)

            if st.checkbox('Do cost to avoid eviction analysis?'):
                evictions_cost_df = cost_of_evictions(df, metro_areas, locations)
                if st.checkbox('Show cost data'):
                    st.dataframe(evictions_cost_df)
                st.markdown(get_table_download_link(evictions_cost_df, state + '_cost_data', 'Download cost data'),
                            unsafe_allow_html=True)

            # output_table(df, 'Output/' + state + '.xlsx')
            # st.success('Data was saved at `' + 'Output/' + state + '.xlsx')
            ranks = rank_counties(df, state).sort_values(by='Relative Risk', ascending=False)
            st.subheader('Ranking')
            st.write('Higher values correspond to more relative risk')
            st.write(ranks['Relative Risk'])
            st.markdown(get_table_download_link(ranks, state + '_ranking', 'Download Relative Risk ranking'),
                        unsafe_allow_html=True)

            visualizations(ranks, state)

        elif task == 'National':
            frames = []
            for state in STATES:
                df = get_state_data(state)
                frames.append(df)
            natl_df = pd.concat(frames)
            if st.checkbox('Show raw data'):
                st.subheader('Raw Data')
                st.dataframe(natl_df)
                st.markdown(get_table_download_link(natl_df, 'national_data', 'Download raw data'),
                            unsafe_allow_html=True)

            if st.checkbox('Do cost to avoid eviction analysis?'):
                evictions_cost_df = cost_of_evictions(natl_df, metro_areas, locations)
                st.markdown(get_table_download_link(evictions_cost_df, 'national_cost', 'Download cost data'),
                            unsafe_allow_html=True)

            ranks = rank_counties(natl_df, 'US_national').sort_values(by='Relative Risk', ascending=False)
            st.subheader('Ranking')
            st.write('Higher values correspond to more relative risk')
            st.write(ranks['Relative Risk'])
            st.markdown(get_table_download_link(natl_df, 'national_ranking', 'Download Relative Risk ranking'),
                        unsafe_allow_html=True)

            # visualizations(natl_df, 'National')
    else:
        st.write('## Data Explorer')
        task = st.selectbox('What type of analysis are you doing?',
                            ['Single County', 'Multiple Counties', 'State', 'National'])
        metro_areas, locations = load_distributions()
        if task == 'Single County' or task == '':
            res = st.text_input('Enter the county and state (ie: Jefferson County, Colorado):')
            if res:
                res = res.strip().split(',')
                county = res[0].strip()
                state = res[1].strip()
                if county and state:
                    df = get_single_county(county, state)
                    st.write(df)
                    if st.checkbox('Show raw data'):
                        st.subheader('Raw Data')
                        st.dataframe(df)
                        st.markdown(
                            get_table_download_link(df, county + '_data', 'Download raw data'),
                            unsafe_allow_html=True)
                    data_explorer(df, state)

        elif task == 'Multiple Counties':
            state = st.selectbox("Select a state", STATES).strip()
            county_list = queries.counties_query()
            county_list = county_list[county_list['State'] == state]['County Name'].to_list()
            counties = st.multiselect('Please specify one or more counties', county_list)
            counties = [_.strip().lower() for _ in counties]
            if len(counties) > 0:
                df = get_multiple_counties(counties, state)

                if st.checkbox('Show raw data'):
                    st.subheader('Raw Data')
                    st.dataframe(df)
                    st.markdown(get_table_download_link(df, state + '_custom_data', 'Download raw data'),
                                unsafe_allow_html=True)
                data_explorer(df, state)
            else:
                st.warning('Select counties to analyze')
                st.stop()

        elif task == 'State':
            state = st.selectbox("Select a state", STATES).strip()
            df = get_state_data(state)

            if st.checkbox('Show raw data'):
                st.subheader('Raw Data')
                st.dataframe(df)
                st.markdown(get_table_download_link(df, state + '_data', 'Download raw data'), unsafe_allow_html=True)
            st.write('''
            ### Scatter Plot
            Select two features to compare on the X and Y axes
            ''')
            data_explorer(df, state)

        elif task == 'National':
            frames = []
            for state in STATES:
                df = get_state_data(state)
                frames.append(df)
            natl_df = pd.concat(frames)
            if st.checkbox('Show raw data'):
                st.subheader('Raw Data')
                st.dataframe(natl_df)
                st.markdown(get_table_download_link(natl_df, 'national_data', 'Download raw data'),
                            unsafe_allow_html=True)
            data_explorer(natl_df, 'national')


if __name__ == '__main__':
    if not os.path.exists('Output'):
        os.makedirs('Output')
    opts, args = getopt.getopt(sys.argv[1:], "hm:", ["mode="])
    mode = None

    for opt, arg in opts:
        if opt == '-h':
            print('run.py -mode <mode>')
            sys.exit()
        elif opt in ("-m", "--mode"):
            mode = arg
            print(mode)

    if mode == 'script':
        MODE = 'SCRIPT'
        task = input(
            'Analyze a single county (1), multiple counties (2), all the counties in a state (3), or a nation-wide analysis (4)? [default: 1]') \
            .strip()
        if task == '1' or task == '':
            res = input('Enter the county and state to analyze (ie: Jefferson County, Colorado):')
            res = res.strip().split(',')
            cost_of_evictions = input(
                'Run an analysis to estimate the cost to avoid evictions? (Y/n) ')
            cost_of_evictions.strip()
            county = res[0].strip().lower()
            state = res[1].strip().lower()
            df = get_single_county(county, state)

            if cost_of_evictions == 'y' or cost_of_evictions == '':
                df = calculate_cost_estimate(df, rent_type='fmr')

            output_table(df, 'Output/' + county.capitalize() + '.xlsx')
            print_summary(df, 'Output/' + county.capitalize() + '.xlsx')
        elif task == '2':
            state = input("Which state are you looking for? (ie: California)").strip()
            counties = input('Please specify one or more counties, separated by commas.').strip().split(',')
            counties = [_.strip().lower() for _ in counties]
            counties = [_ + ' county' for _ in counties if ' county' not in _]
            df = get_multiple_counties(counties, state)
            cost_of_evictions = input(
                'Run an analysis to estimate the cost to avoid evictions? (Y/n) ')
            if cost_of_evictions == 'y' or cost_of_evictions == '':
                df = calculate_cost_estimate(df, rent_type='fmr')

            output_table(df, 'Output/' + state + '_selected_counties.xlsx')
            analysis_df = rank_counties(df, state + '_selected_counties')
            print_summary(analysis_df, 'Output/' + state + '_selected_counties.xlsx')
        elif task == '3':
            state = input("Which state are you looking for? (ie: California)").strip()
            df = get_state_data(state)
            cost_of_evictions = input(
                'Run an analysis to estimate the cost to avoid evictions? (Y/n) ')
            if cost_of_evictions == 'y' or cost_of_evictions == '':
                df = calculate_cost_estimate(df, rent_type='fmr')

            output_table(df, 'Output/' + state + '.xlsx')
            analysis_df = rank_counties(df, state)
            print_summary(analysis_df, 'Output/' + state + '.xlsx')
            temp = df.copy()
            temp.reset_index(inplace=True)
            counties = temp['County Name'].to_list()
            geom = queries.get_county_geoms(counties, state.lower())
            df = df.merge(geom, on='County Name', how='outer')

        elif task == '4':
            frames = []
            for state in STATES:
                df = get_state_data(state)
                frames.append(df)
            natl_df = pd.concat(frames)
            cost_of_evictions = input(
                'Run an analysis to estimate the cost to avoid evictions (Y/n) ')
            if cost_of_evictions == 'y' or cost_of_evictions == '':
                df = calculate_cost_estimate(natl_df, rent_type='fmr')

            output_table(natl_df, 'Output/US_national.xlsx')
            analysis_df = rank_counties(natl_df, 'US_national')
            print_summary(analysis_df, 'Output/US_national.xlsx')
        else:
            raise Exception('INVALID INPUT! Enter a valid task number.')
    else:
        run_UI()
