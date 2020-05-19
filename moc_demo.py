import pandas as pd
import numpy as np
import requests
import sqlalchemy as sa
from datetime import timedelta

from prefect import task, Flow, Parameter, unmapped
from prefect.engine.results import LocalResult
from prefect.engine.state import Success, Failed, Skipped

from prefect.schedules import clocks, filters, Schedule, IntervalSchedule
import pendulum

from prefect.tasks.secrets.base import PrefectSecret

import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# s3_handler = S3ResultHandler(bucket='tsx-moc-bcp')
# # https://docs.prefect.io/api/latest/utilities/context.html#context-2  
lcl_handler = LocalResult(dir="/home/ilivni/prefect_guide/results/")

def error_notifcation_handler(obj, old_state, new_state):
    # Hamdle an empty dataframe to return a fail message.  
    # The result of a succesfull 
    if new_state.is_failed():
        p = PrefectSecret("system_errors") 
        slack_web_hook_url = p.run()

        msg = f"Task '{obj.name}' finished in state {new_state.message}"
        # replace URL with your Slack webhook URL
        requests.post(slack_web_hook_url, json={"text": msg})
                
    else:
        return_state = new_state   
        
    return return_state



def imb_handler(obj, old_state, new_state):
    # Hamdle an empty dataframe to return a fail message.  
    # The result of a succesfull 
    if isinstance(new_state, Success) and new_state.result.empty:
        return_state = Failed(
            message=f"No tsx imbalance data: No trading or Data was not published to {new_state.cached_inputs['url']}", 
            result=new_state.result
            )
        raise signals.SKIP(message='See Error Msg')        
    else:
        return_state = new_state   

    return return_state

@task(
    max_retries=2, 
    retry_delay=timedelta(seconds=1),
    result=lcl_handler,
    target="{today}/{task_name}.prefect",
    state_handlers=[imb_handler, error_notifcation_handler]
    )
def get_tsx_moc_imb(url: str):
    """
    Scrape the TSX website Market on close website. Data only available weekdays after 15:40 pm Toronto time
    until 12 am.
    
    Use archived url for testing.       
    "https://web.archive.org/web/20200414202757/https://api.tmxmoney.com/mocimbalance/en/TSX/moc.html"
    """

    raise Exception
    
    # 1, Get the html content
    html = requests.get(url).content
    
    # 2. Read all the tables
    df_list = pd.read_html(html, header=[0], na_values=[''], keep_default_na=False)
    
    tsx_imb_df = df_list[-1]
    
    logger.info(f"MOC download shape {tsx_imb_df.shape}")

    tsx_imb_df = tsx_imb_df.set_index('Symbol')

    return tsx_imb_df#.head(0)

@task(state_handlers=[error_notifcation_handler])
def partition_df(df, n_conn=1):
    df_lst = np.array_split(df, n_conn)
    return df_lst


@task(state_handlers=[error_notifcation_handler])
def df_to_db(df, tbl_name, conn_str):
    #raise Exception
    engine = sa.create_engine(conn_str)
    
    # Changer "if_exist" to "append" when done devolpment
    df.to_sql(name=tbl_name, con=engine, if_exists="append", index=True, method="multi")
    
    engine.dispose()
  
    return df.shape


# A flow has no particular order unless the data is bound (shown) or explicitly set (not shown).
with Flow(name="Get-TSX-MOC-Imbalances") as tsx_imb_fl:
    
    tsx_url = Parameter("tsx_url", default="https://api.tmxmoney.com/mocimbalance/en/TSX/moc.html")
    imb_tbl_nm = Parameter("imb_tbl_nm", default="moc_tst")
    n_conn = Parameter("n_conn", default=1) 
    
    tsx_imb_df = get_tsx_moc_imb(tsx_url)

    conn_str = PrefectSecret("moc_pgdb_conn")
    
    tsx_imb_df_lst = partition_df(tsx_imb_df, n_conn)

    df_shape = df_to_db.map(tsx_imb_df_lst, tbl_name=unmapped(imb_tbl_nm), conn_str=unmapped(conn_str))

if __name__ == "__main__":

    # Inputs
    tsx_url = 'https://api.tmxmoney.com/mocimbalance/en/TSX/moc.html'
    backup_url = "https://web.archive.org/web/20200414202757/https://api.tmxmoney.com/mocimbalance/en/TSX/moc.html"

    # Script
    from prefect.engine.executors import LocalExecutor

    #from prefect.environments import RemoteEnvironment
    #tsx_imb_fl.environment=RemoteEnvironment(executor="prefect.engine.executors.LocalExecutor")

    schedule = Schedule(
        # fire every day
        clocks=[clocks.IntervalClock(
            start_date=pendulum.datetime(2020, 4, 22, 22, 30, tz="America/Toronto"),
            interval=timedelta(days=1)
            )],
        # but only on weekdays
        filters=[filters.is_weekday],

        # and not in January TODO: Add TSX Holidays
        not_filters=[filters.between_dates(1, 1, 1, 31)]
)

    tsx_imb_fl.schedule = schedule

    tsx_imb_fl.visualize()
    fl_state = tsx_imb_fl.run(
        parameters=dict(
            tsx_url=backup_url,
            n_conn=4
        ), 
        executor=LocalExecutor()

    )
    tsx_imb_fl.visualize(flow_state=fl_state)