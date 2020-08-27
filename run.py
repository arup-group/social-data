import itertools
import math
import os

import pandas as pd
import sklearn.preprocessing as pre

import api
import queries

# Pandas options
pd.set_option('max_rows', 10)
pd.set_option('max_columns', 10)
pd.set_option('expand_frame_repr', True)
pd.set_option('large_repr', 'truncate')
pd.options.display.float_format = '{:.2f}'.format

HOUSING_STOCK_DISTRIBUTION = { # Assumed National housing distribution [source]
    0: 0.0079,
    1: 0.1083,
    2: 0.2466,
    3: 0.4083,
    4: 0.2289
}
# todo: turn this into a constant as well and document
BURDENED_HOUSEHOLD_PROPORTION = [5, 25, 33, 50, 75]

def filter_state(data: pd.DataFrame, state: str) -> pd.DataFrame:
    return data[data['State'].str.lower() == state.lower()]


def filter_counties(data: pd.DataFrame, counties: list) -> pd.DataFrame:
    return data[data['County Name'].str.lower().isin(counties)]


def clean_fred_data(data: pd.DataFrame) -> pd.DataFrame:
    data['Non-Home Ownership (%)'] = 100 - data['Home Ownership (%)']

    data.drop([
        'Home Ownership (%)',
        'county_id',
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
    df[name] = (df[feature] / 100) * df['Resident Population (Thousands of Persons)'] * 1000
    return df


def cross_features(df: pd.DataFrame) -> pd.DataFrame:
    cols = ['Pop Below Poverty Level', 'Pop Unemployed', 'Income Inequality (Ratio)', 'Non-Home Ownership Pop',
            'Num Burdened Households', 'Num Single Parent Households']
    all_combinations = []
    for r in range(2, len(cols)):
        combinations_list = list(itertools.combinations(cols, r))
        all_combinations += combinations_list
    all_combinations.pop(0)
    new_cols = []
    for combo in all_combinations:
        new_cols.append(cross(combo, df))

    crossed_df = pd.DataFrame(new_cols)
    crossed_df = crossed_df.T
    crossed_df['Mean'] = crossed_df.mean(axis=1)
    crossed_df.to_excel('Output/data_crossed.xlsx')

    return crossed_df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = percent_to_population('Population Below Poverty Line (%)', 'Pop Below Poverty Level', df)
    df = percent_to_population('Unemployment Rate (%)', 'Pop Unemployed', df)
    df = percent_to_population('Burdened Households (%)', 'Num Burdened Households', df)
    df = percent_to_population('Single Parent Households (%)', 'Num Single Parent Households', df)
    df = percent_to_population('Non-Home Ownership (%)', 'Non-Home Ownership Pop', df)

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


def priority_indicator(socioeconomic_index: float, policy_index: float, time_left: float = 1) -> float:
    return socioeconomic_index * (1 - policy_index) / math.sqrt(time_left)


def rank_counties(df: pd.DataFrame, label: str) -> pd.DataFrame:
    policy_df = api.get_from_excel('Policy Workbook.xlsx', 'Analysis Data')
    analysis_df = normalize(df)
    crossed = cross_features(analysis_df)
    analysis_df['Crossed'] = crossed['Mean']
    analysis_df = normalize_column(analysis_df, 'Crossed')

    analysis_df['Relative Risk'] = analysis_df.sum(axis=1)
    max_sum = analysis_df['Relative Risk'].max()
    analysis_df['Relative Risk'] = (analysis_df['Relative Risk'] / max_sum)
    counties = df.index.levels[1].values

    if not any(policy_df['County Name'].isin(counties)):
        print('Selected counties are not in the policy data! Fill out `Policy Workbook.xlsx` for the desired counties')
    else:
        analysis_df['Policy Index'] = policy_df['Policy Value'].copy()
        analysis_df['Countdown'] = policy_df['Countdown'].copy()
        analysis_df['Rank'] = analysis_df.apply(
            lambda x: priority_indicator(x['Relative Risk'], x['Policy Index'], x['Countdown']), axis=1
        )

    analysis_df.to_excel('Output/' + label + '_overall_vulnerability.xlsx')

    return analysis_df


def load_all_data() -> pd.DataFrame:
    if os.path.exists("Output/all_tables.xlsx"):
        try:
            print('Using local `all_tables.xlsx`')
            df = pd.read_excel('Output/all_tables.xlsx')
        except:
            print('Something went wrong with the Excel file. Falling back to database query.')
            df = queries.latest_data_all_tables()
    else:
        df = queries.latest_data_all_tables()

    return df


def get_single_county(county: str, state: str) -> pd.DataFrame:
    df = load_all_data()
    df = clean_fred_data(df)

    df = filter_state(df, state)
    df = filter_counties(df, [county])
    df.set_index(['County Name'], drop=True, inplace=True)
    return df


def get_multiple_counties(counties: list, state: str) -> pd.DataFrame:
    df = load_all_data()
    df = clean_fred_data(df)

    df = filter_state(df, state)
    df = filter_counties(df, counties)
    df.set_index(['State', 'County Name'], drop=True, inplace=True)

    return df


def get_state_data(state: str) -> pd.DataFrame:
    df = load_all_data()
    df = clean_fred_data(df)

    df = filter_state(df, state)
    df.set_index(['State', 'County Name'], drop=True, inplace=True)

    return df


def output_table(df: pd.DataFrame, path: str):
    df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
    df.to_excel(path)

def join_fmr_data(county, state):
    all_other_data = load_all_data()
    df = queries.static_data_single_table('fair_market_rents', queries.static_columns['fair_market_rents'])
    df=df.drop([
        'State',
        'County Name'
    ], axis=1)
    df = pd.merge(all_other_data, df, on='county_id')
    df = filter_state(df, state)
    df = filter_counties(df, [county])
    print(df.head())
    df.set_index(['State', 'County Name'], drop=True, inplace=True)
    df=df.drop([
        'Burdened Households Date',
        'Home Ownership Date',
        'Income Inequality Date',
        'Population Below Poverty Line Date',
        'Single Parent Households Date',
        'SNAP Benefits Recipients Date',
        'Unemployment Rate Date',
        'Resident Population Date'
    ], axis=1)
    df=df.astype(float)
    print(df.head())
    for key, value in HOUSING_STOCK_DISTRIBUTION.items():
        for pro in BURDENED_HOUSEHOLD_PROPORTION:
            df[str(key)+'_br_cost_'+str(pro)] = value * df['fmr_0'] * (pro/100) * (df['Resident Population (Thousands of Persons)']*1000) * (df['Burdened Households (%)']/100)
            df[str(key)+'_br_cost_'+str(pro)] = value * df['fmr_1'] * (pro/100) * (df['Resident Population (Thousands of Persons)']*1000) * (df['Burdened Households (%)']/100)
            df[str(key)+'_br_cost_'+str(pro)] = value * df['fmr_2'] * (pro/100) * (df['Resident Population (Thousands of Persons)']*1000) * (df['Burdened Households (%)']/100)
            df[str(key)+'_br_cost_'+str(pro)] = value * df['fmr_3'] * (pro/100) * (df['Resident Population (Thousands of Persons)']*1000) * (df['Burdened Households (%)']/100)
            df[str(key)+'_br_cost_'+str(pro)] = value * df['fmr_4'] * (pro/100) * (df['Resident Population (Thousands of Persons)']*1000) * (df['Burdened Households (%)']/100)
    return df

# def get_cost_of_evictions(county, state):


if __name__ == '__main__':
    if not os.path.exists('Output'):
        os.makedirs('Output')

    task = input('Analyze a single county (1), multiple counties (2), or all the counties in a state (3)? [default: 1]') \
        .strip()

    if task == '1' or task == '':
        res = input('Enter the county and state (ie: Jefferson County, Colorado):')
        res = res.strip().split(',')
        cost_of_evictions = input('Are you also interested in running a cost of evictions analysis for your chosen county? (y/n) ')
        cost_of_evictions.strip()
        county = res[0].strip()
        state = res[1].strip()
        df = get_single_county(county, state)
        if cost_of_evictions == 'y':
            join_fmr_data(county, state)
            # get_cost_of_evictions(county, state)
        output_table(df, 'Output/' + county + '.xlsx')
    elif task == '2':
        state = input("Which state are you looking for (ie: California)?]").strip()
        counties = input('Please specify one or more counties, separated by commas [ie: ].').strip().split(',')
        counties = [_.strip().lower() for _ in counties]
        counties = [_ + ' county' for _ in counties if ' county' not in _]
        df = get_multiple_counties(counties, state)
        output_table(df, 'Output/' + state + '_selected_counties.xlsx')
        rank_counties(df, state + '_selected_counties')
    elif task == '3':
        state = input("Which state are you looking for (ie: California)?]").strip()
        df = get_state_data(state)
        output_table(df, 'Output/' + state + '.xlsx')
        rank_counties(df, state)
    else:
        raise Exception('INVALID INPUT! Enter a valid task number.')
