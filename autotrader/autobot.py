import os
import importlib
import numpy as np
import pandas as pd
from datetime import datetime
from autotrader.comms import emailing
from autotrader.autodata import GetData
from autotrader.brokers.trading import Order
from autotrader.utilities import read_yaml, get_config


class AutoTraderBot:
    """AutoTrader Trading Bot.
    
    Attributes
    ----------
    instrument : str
        The trading instrument assigned to the bot.
    data : pd.DataFrame
        The OHLC price data used by the bot.
    quote_data : pd.DataFrame
        The OHLC quote data used by the bot.
    MTF_data : dict
        The multiple timeframe data used by the bot.
    backtest_summary : dict
        A dictionary containing results from the bot in backtest. This 
        dictionary is available only after a backtest and has keys: 'data', 
        'account_history', 'trade_summary', 'indicators', 'instrument', 
        'interval', 'open_trades', 'cancelled_trades'.
    
    """
    
    def __init__(self, instrument: str, strategy_dict: dict, 
                 broker, data_dict: dict, quote_data_path: str, 
                 auxdata: dict, autotrader_instance) -> None:
        """Instantiates an AutoTrader Bot.

        Parameters
        ----------
        instrument : str
            The trading instrument assigned to the bot instance.
        strategy_dict : dict
            The strategy configuration dictionary.
        broker : AutoTrader Broker instance
            The AutoTrader Broker module.
        data_dict : dict
            The strategy data.
        quote_data_path : str
            The quote data filepath for the trading instrument 
            (for backtesting only).
        auxdata : dict
            Auxiliary strategy data.
        autotrader_instance : AutoTrader
            The parent AutoTrader instance.

        Raises
        ------
        Exception
            When there is an error retrieving the instrument data.

        Returns
        -------
        None
            The trading bot will be instantiated and ready for trading.

        """
        # Inherit user options from autotrader
        for attribute, value in autotrader_instance.__dict__.items():
            setattr(self, attribute, value)
        self._scan_results = {}
        
        # Assign local attributes
        self.instrument = instrument
        self._broker = broker
        
        # Unpack strategy parameters and assign to strategy_params
        # TODO - clean this up
        strategy_config = strategy_dict['config']
        interval = strategy_config["INTERVAL"]
        period = strategy_config["PERIOD"]
        risk_pc = strategy_config["RISK_PC"] if 'RISK_PC' in strategy_config \
            else None
        sizing = strategy_config["SIZING"] if 'SIZING' in strategy_config \
            else None
        params = strategy_config["PARAMETERS"]
        strategy_params = params
        strategy_params['granularity'] = strategy_params['granularity'] \
            if 'granularity' in strategy_params else interval
        strategy_params['risk_pc'] = strategy_params['risk_pc'] \
            if 'risk_pc' in strategy_params else risk_pc
        strategy_params['sizing'] = strategy_params['sizing'] \
            if 'sizing' in strategy_params else sizing
        strategy_params['period'] = strategy_params['period'] \
            if 'period' in strategy_params else period
        strategy_params['INCLUDE_POSITIONS'] = strategy_config['INCLUDE_POSITIONS'] \
            if 'INCLUDE_POSITIONS' in strategy_config else True
        self._strategy_params = strategy_params
        
        # Import Strategy
        if strategy_dict['class'] is not None:
            strategy = strategy_dict['class']
        else:
            strat_module = strategy_config["MODULE"]
            strat_name = strategy_config["CLASS"]
            strat_package_path = os.path.join(self._home_dir, "strategies") 
            strat_module_path = os.path.join(strat_package_path, 
                                             strat_module) + '.py'
            strat_spec = importlib.util.spec_from_file_location(strat_module, 
                                                                strat_module_path)
            strategy_module = importlib.util.module_from_spec(strat_spec)
            strat_spec.loader.exec_module(strategy_module)
            strategy = getattr(strategy_module, strat_name)
        
        # Get broker configuration 
        global_config_fp = os.path.join(self._home_dir, 'config', 
                                        'GLOBAL.yaml')
        if os.path.isfile(global_config_fp):
            global_config = read_yaml(global_config_fp)
        else:
            global_config = None
        broker_config = get_config(self._environment, global_config, self._feed)
   
        # Data retrieval
        self._quote_data_file = quote_data_path     # Either str or None
        self._data_filepaths = data_dict            # Either str or dict, or None
        self._auxdata_files = auxdata
        
        # Fetch data
        self._get_data = GetData(broker_config, self._allow_dancing_bears,
                                 self._base_currency)
        self._refresh_data()
        
        # Instantiate Strategy
        include_broker = strategy_config['INCLUDE_BROKER'] \
            if 'INCLUDE_BROKER' in strategy_config else False
        if include_broker:
            my_strat = strategy(params, self._strat_data, instrument, 
                                self._broker, self._broker_utils)
        else:
            my_strat = strategy(params, self._strat_data, instrument)
            
        # Assign strategy to local attributes
        self._last_bar = None
        self._strategy = my_strat
        self._latest_orders = []
        
        # Assign strategy attributes for tick-based strategy development
        if self._backtest_mode:
            self._strategy._backtesting = True
            self.backtest_summary = None
        if interval.split(',')[0] == 'tick':
            self._strategy._tick_data = True
        
        if int(self._verbosity) > 0:
                print("\nAutoTraderBot assigned to trade {}".format(instrument),
                      "on {} timeframe using {}.".format(self._strategy_params['granularity'],
                                                         strategy_config['NAME']))
    
    
    def __repr__(self):
        return f'{self.instrument} AutoTraderBot'
    
    
    def __str__(self):
        return 'AutoTraderBot instance'
    
    
    def _refresh_data(self):
        
        # TODO - allow user to pass in custom data retrieval object, to 
        # customise how data is delivered. The retrieve_data method (and others)
        # could be abstracted away into their own class, which could be inherited
        # by user feeds. The current timestamp should be passed into this method,
        # as well as retrive data method, to allow data retrieval based 
        # on timestamp (think contracts with expiries)
        
        # Fetch new data
        data, multi_data, quote_data, auxdata = self._retrieve_data(self.instrument, self._feed)
        
        # Check data returned is valid
        if len(data) == 0:
            raise Exception("Error retrieving data.")
        
        # Data assignment
        if multi_data is None:
            strat_data = data
        else:
            strat_data = multi_data
        
        # Auxiliary data assignment
        if auxdata is not None:
            strat_data = {'base': strat_data,
                          'aux': auxdata}
        
        # Assign data attributes to bot
        self._strat_data = strat_data
        self.data = data
        self.multi_data = multi_data
        self.auxdata = auxdata
        self.quote_data = quote_data
        
        # strat_data will either contain a single timeframe OHLC dataframe, 
        # a dictionary of MTF dataframes, or a dict with 'base' and 'aux' keys,
        # for aux and base strategy data (which could be single of MTF).
        
    
    def _retrieve_data(self, instrument: str, feed: str) -> pd.DataFrame:
        
        # Retrieve main data
        if self._data_filepaths is not None:
            # Local data filepaths provided
            if isinstance(self._data_filepaths, str):
                # Single data filepath provided
                data = self._get_data.local(self._data_filepaths, self._data_start, 
                                            self._data_end)
                multi_data = None
                
            elif isinstance(self._data_filepaths, dict):
                # Multiple data filepaths provided
                multi_data = {}
                for granularity, filepath in self._data_filepaths.items():
                    data = self._get_data.local(filepath, self._data_start, self._data_end)
                    multi_data[granularity] = data
                
                # Extract first dataset as base data
                data = multi_data[list(self._data_filepaths.keys())[0]]
        
        else:
            # Download data
            multi_data = {}
            for granularity in self._strategy_params['granularity'].split(','):
                data_func = getattr(self._get_data, feed.lower())
                data = data_func(instrument, granularity=granularity, 
                                 count=self._strategy_params['period'], 
                                 start_time=self._data_start,
                                 end_time=self._data_end)
                
                multi_data[granularity] = data
            
            data = multi_data[self._strategy_params['granularity'].split(',')[0]]
            
            if len(multi_data) == 1:
                multi_data = None
        
        # Retrieve quote data
        if self._quote_data_file is not None:
            quote_data = self._get_data.local(self._quote_data_file, 
                                              self._data_start, self._data_end)
        else:
            quote_data_func = getattr(self._get_data,f'_{feed.lower()}_quote_data')
            quote_data = quote_data_func(data, instrument, 
                                         self._strategy_params['granularity'].split(',')[0], 
                                         self._data_start, self._data_end)
        
        # Retrieve auxiliary data
        if self._auxdata_files is not None:
            if isinstance(self.auxdata, str):
                # Single data filepath provided
                auxdata = self._get_data.local(self.auxdata, self._data_start, 
                                               self._data_end)
                
            elif isinstance(self.auxdata, dict):
                # Multiple data filepaths provided
                auxdata = {}
                for key, filepath in self.auxdata.items():
                    data = self._get_data.local(filepath, self._data_start, self._data_end)
                    auxdata[key] = data
        else:
            auxdata = None
        
        # Correct any data mismatches
        data, quote_data = self._match_quote_data(data, quote_data)
        
        return data, multi_data, quote_data, auxdata
        
    
    @staticmethod
    def _check_data_period(data: pd.DataFrame, from_date: datetime, 
                           to_date: datetime) -> pd.DataFrame:
        """Checks and returns the dataset matching the backtest start and 
        end dates (as close as possible).
        """
        return data[(data.index >= from_date) & (data.index <= to_date)]
        

    def _update(self, i: int = None, timestamp: datetime = None) -> None:
        """Update strategy with the latest data and generate a trade signal.
        """
        # Reset latest orders
        self._latest_orders = []
        
        if self._run_mode == 'continuous':
            # Running in continuous update mode
            strat_data, current_bar, quote_bar, sufficient_data = self._check_data(timestamp, self._data_indexing)
            
        else:
            # Running in periodic update mode
            if self._strategy_params['INCLUDE_POSITIONS']:
                current_position = self._broker.get_positions(self.instrument)
            else:
                current_position = None
            
            # Assign current bars
            current_bar = self.data.iloc[i]
            quote_bar = self.quote_data.iloc[i]
            sufficient_data = True
        
        # Check for duplicated data
        duplicate_data = self._check_last_bar(current_bar)
        
        if sufficient_data and not duplicate_data:
            # Update backtest
            if self._backtest_mode:
                self._update_backtest(current_bar)
            
            # Get strategy orders
            if self._run_mode == 'continuous':
                strategy_orders = self._strategy.generate_signal(strat_data)
            else:
                strategy_orders = self._strategy.generate_signal(i, current_position=current_position)
            
            # Check and qualify orders
            orders = self._check_orders(strategy_orders)
            self._qualify_orders(orders, current_bar, quote_bar)
            
            # Submit orders
            for order in orders:
                if self._scan_mode:
                    # Bot is scanning
                    scan_hit = {"size"  : order.size,
                                "entry" : current_bar.Close,
                                "stop"  : order.stop_loss,
                                "take"  : order.take_profit,
                                "signal": order.direction}
                    self._scan_results[self.instrument] = scan_hit
                    pass
                    
                else:
                    # Bot is trading
                    self._broker.place_order(order, order_time=current_bar.name)
                    self._latest_orders.append(order)
            
            if int(self._verbosity) > 1:
                if len(self._latest_orders) > 0:
                    for order in self._latest_orders:
                        order_string = "{}: {} {}".format(order.order_time.strftime("%b %d %Y %H:%M:%S"), 
                                                          order.instrument, 
                                                          order.order_type) + \
                            " order of {} units placed at {}.".format(order.size,
                                                                      order.order_price)
                        print(order_string)
                else:
                    if int(self._verbosity) > 2:
                        print("{}: No signal detected ({}).".format(current_bar.name.strftime("%b %d %Y %H:%M:%S"),
                                                                    self.instrument))
            
            # Check for orders placed and/or scan hits
            if int(self._notify) > 0 and not self._backtest_mode:
                
                for order_details in self._latest_orders:
                    self._broker_utils.write_to_order_summary(order_details, 
                                                             self._order_summary_fp)
                
                if int(self._notify) > 1 and \
                    self._email_params['mailing_list'] is not None and \
                    self._email_params['host_email'] is not None:
                        if int(self._verbosity) > 0 and len(self._latest_orders) > 0:
                                print("Sending emails ...")
                                
                        for order_details in self._latest_orders:
                            emailing.send_order(order_details,
                                                self._email_params['mailing_list'],
                                                self._email_params['host_email'])
                            
                        if int(self._verbosity) > 0 and len(self._latest_orders) > 0:
                                print("  Done.\n")
                
            # Check scan results
            if self._scan_mode:
                # Construct scan details dict
                scan_details    = {'index'      : self._scan_index,
                                   'strategy'   : self._strategy.name,
                                   'timeframe'  : self._strategy_params['granularity']
                                   }
                
                # Report AutoScan results
                # Scan reporting with no emailing requested.
                if int(self._verbosity) > 0 or \
                    int(self._notify) == 0:
                    if len(self._scan_results) == 0:
                        print("{}: No signal detected.".format(self.instrument))
                    else:
                        # Scan detected hits
                        for instrument in self._scan_results:
                            signal = self._scan_results[instrument]['signal']
                            signal_type = 'Long' if signal == 1 else 'Short'
                            print(f"{instrument}: {signal_type} signal detected.")
                
                if int(self._notify) > 0:
                    # Emailing requested
                    if len(self._scan_results) > 0 and \
                        self._email_params['mailing_list'] is not None and \
                        self._email_params['host_email'] is not None:
                        # There was a scanner hit and email information is provided
                        emailing.send_scan_results(self._scan_results, 
                                                   scan_details, 
                                                   self._email_params['mailing_list'],
                                                   self._email_params['host_email'])
                    elif int(self._notify) > 1 and \
                        self._email_params['mailing_list'] is not None and \
                        self._email_params['host_email'] is not None:
                        # There was no scan hit, but notify set > 1, so send email
                        # regardless.
                        emailing.send_scan_results(self._scan_results, 
                                                   scan_details, 
                                                   self._email_params['mailing_list'],
                                                   self._email_params['host_email'])
                    
    
    def _check_orders(self, orders) -> list:
        """Checks that orders returned from strategy are in the correct
        format.
        
        Returns
        -------
        List of Orders
        
        Notes
        -----
        An order must have (at the very least) an order type specified. Usually,
        the direction will also be required, except in the case of close order 
        types. If an order with no order type is provided, it will be ignored.
        """
        
        def check_type(orders):
            checked_orders = []
            if isinstance(orders, dict):
                # Order(s) provided in dictionary
                if 'order_type' in orders:
                    # Single order dict provided
                    if 'instrument' not in orders:
                        orders['instrument'] = self.instrument
                    checked_orders.append(Order._from_dict(orders))
                    
                elif len(orders) > 0:
                    # Multiple orders provided
                    for key, item in orders.items():
                        if isinstance(item, dict) and 'order_type' in item:
                            # Convert order dict to Order object
                            if 'instrument' not in item:
                                item['instrument'] = self.instrument
                            checked_orders.append(Order._from_dict(item))
                        elif isinstance(item, Order):
                            # Native Order object, append as is
                            checked_orders.append(item)
                        else:
                            raise Exception(f"Invalid order submitted: {item}")
                
                elif len(orders) == 0:
                    # Empty order dict
                    pass
                
            elif isinstance(orders, Order):
                # Order object directly returned
                checked_orders.append(orders)
                
            elif isinstance(orders, list):
                # Order(s) provided in list
                for item in orders:
                    if isinstance(item, dict) and 'order_type' in item:
                        # Convert order dict to Order object
                        if 'instrument' not in item:
                            item['instrument'] = self.instrument
                        checked_orders.append(Order._from_dict(item))
                    elif isinstance(item, Order):
                        # Native Order object, append as is
                        checked_orders.append(item)
                    else:
                        raise Exception(f"Invalid order submitted: {item}")
            else:
                raise Exception(f"Invalid order submitted: {item}")
            
            return checked_orders
        
        def add_strategy_data(orders):
            # Append strategy parameters to each order
            for order in orders:
                order.instrument = self.instrument if not order.instrument else order.instrument
                order.strategy = self._strategy.name
                order.granularity = self._strategy_params['granularity']
                order._sizing = self._strategy_params['sizing']
                order._risk_pc = self._strategy_params['risk_pc']
                
        def check_order_details(orders: list) -> None:
            for ix, order in enumerate(orders):
                order.instrument = order.instrument if order.instrument is not None else self.instrument
                if order.order_type in ['market', 'limit', 'stop-limit', 'reduce']:
                    if not order.direction:
                        del orders[ix]
                        if self._verbosity > 1:
                            print("No trade direction provided for " + \
                                  f"{order.order_type} order. Order will be ignored.")
        
        # Perform checks
        checked_orders = check_type(orders)
        add_strategy_data(checked_orders)
        check_order_details(checked_orders)
        
        return checked_orders
        
    
    def _qualify_orders(self, orders: list, current_bar: pd.core.series.Series,
                        quote_bar: pd.core.series.Series) -> None:
        """Passes price data to order to populate missing fields.
        """
        for order in orders:
            if self._req_liveprice:
                liveprice_func = getattr(self._get_data, f'{self._feed.lower()}_liveprice')
                last_price = liveprice_func(order)
            else:
                last_price = self._get_data._pseduo_liveprice(last=current_bar.Close,
                                                              quote_price=quote_bar.Close)
            
            if order.direction < 0:
                order_price = last_price['bid']
                HCF = last_price['negativeHCF']
            else:
                order_price = last_price['ask']
                HCF = last_price['positiveHCF']
            
            # Call order with price and time
            order(broker=self._broker, order_price=order_price, HCF=HCF)
    
    
    def _update_backtest(self, current_bar: pd.core.series.Series) -> None:
        """Updates virtual broker with latest price data for backtesting.
        """
        self._broker._update_positions(current_bar, self.instrument)
    
    
    def _create_backtest_summary(self, balance: pd.Series, NAV: pd.Series, 
                                margin: pd.Series, trade_times = None) -> dict:
        """Constructs backtest summary dictionary for further processing.
        """
        trade_summary = self._broker_utils.trade_summary(trades=self._broker.trades,
                                                         instrument=self.instrument)
        order_summary = self._broker_utils.trade_summary(orders=self._broker.orders,
                                                         instrument=self.instrument)
        
        if trade_times is None:
            trade_times = self.data.index
        
        # closed_trades = trade_summary[trade_summary.status == 'closed']
        open_trade_summary = trade_summary[trade_summary.status == 'open']
        cancelled_summary = order_summary[order_summary.status == 'cancelled']
        
        backtest_dict = {}
        backtest_dict['data'] = self.data
        backtest_dict['account_history'] = pd.DataFrame(data={'balance': balance, 
                                                              'NAV': NAV, 
                                                              'margin': margin,
                                                              'drawdown': np.array(NAV)/np.maximum.accumulate(NAV) - 1}, 
                                                        index=trade_times)
        backtest_dict['trade_summary'] = trade_summary
        backtest_dict['indicators'] = self._strategy.indicators if hasattr(self._strategy, 'indicators') else None
        backtest_dict['instrument'] = self.instrument
        backtest_dict['interval'] = self._strategy_params['granularity']
        backtest_dict['open_trades'] = open_trade_summary
        backtest_dict['cancelled_trades'] = cancelled_summary
        
        self.backtest_summary = backtest_dict
    
    
    def _get_iteration_range(self) -> int:
        """Checks mode of operation and returns data iteration range. For backtesting,
        the entire dataset is iterated over. For livetrading, only the latest candle
        is used. ONLY USED IN BACKTESTING NOW.
        """
        
        start_range = self._strategy_params['period']
        end_range = len(self.data)
        
        if len(self.data) < start_range:
            raise Exception("There are not enough bars in the data to " + \
                            "run the backtest with the current strategy " + \
                            "configuration settings. Either extend the " + \
                            "backtest period, or reduce the PERIOD key of " + \
                            "your strategy configuration.")
        
        return start_range, end_range
    
    
    def _match_quote_data(self, data: pd.DataFrame, 
                          quote_data: pd.DataFrame) -> pd.DataFrame:
        """Function to match index of trading data and quote data.
        """
        datasets = [data, quote_data]
        adjusted_datasets = []
        
        for dataset in datasets:
            # Initialise common index
            common_index = dataset.index
            
            # Update common index by intersection with other data 
            for other_dataset in datasets:
                common_index = common_index.intersection(other_dataset.index)
            
            # Adjust data using common index found
            adj_data = dataset[dataset.index.isin(common_index)]
            
            adjusted_datasets.append(adj_data)
        
        # Unpack adjusted datasets
        adj_data, adj_quote_data = adjusted_datasets
        
        return adj_data, adj_quote_data
    
    
    @staticmethod
    def _check_ohlc_data(ohlc_data: pd.DataFrame, timestamp: datetime, 
                         indexing: str = 'open', tail_bars: int = None,
                         check_for_future_data: bool = True) -> pd.DataFrame:
        """Checks the index of inputted data to ensure it contains no future 
        data.

        Parameters
        ----------
        ohlc_data : pd.DataFrame
            DESCRIPTION.
        timestamp : datetime
            DESCRIPTION.
        indexing : str, optional
            How the OHLC data has been indexed (either by bar 'open' time, or
            bar 'close' time). The default is 'open'.
        tail_bars : int, optional
            If provided, the data will be truncated to provide the number
            of bars specified. The default is None.
        
        Raises
        ------
        Exception
            DESCRIPTION.

        Returns
        -------
        past_data : TYPE
            DESCRIPTION.

        """
        if check_for_future_data:
            if indexing.lower() == 'open':
                past_data = ohlc_data[ohlc_data.index < timestamp]
            elif indexing.lower() == 'close':
                past_data = ohlc_data[ohlc_data.index <= timestamp]
            else:
                raise Exception(f"Unrecognised indexing type '{indexing}'.")
        
        if tail_bars is not None:
            past_data = past_data.tail(tail_bars)
            
        return past_data
    
    
    def _check_auxdata(self, auxdata: dict, timestamp: datetime, 
                       indexing: str = 'open', tail_bars: int = None,
                       check_for_future_data: bool = True) -> dict:
        processed_auxdata = {}
        for key, item in auxdata.items():
            if isinstance(item, pd.DataFrame) or isinstance(item, pd.Series):
                processed_auxdata[key] = self._check_ohlc_data(item, timestamp, 
                                    indexing, tail_bars, check_for_future_data)
            else:
                processed_auxdata[key] = item
        return processed_auxdata
                
    
    def _check_data(self, timestamp: datetime, indexing: str = 'open') -> dict:
        """Wrapper for multiple datasets contained in a dictionary.

        Parameters
        ----------
        timestamp : datetime
            DESCRIPTION.
        indexing : str, optional
            DESCRIPTION. The default is 'open'.

        Returns
        -------
        checked_data : TYPE
            DESCRIPTION.

        """
        def get_current_bar(data):
            if len(data) > 0:
                current_bar = data.iloc[-1]
            else:
                current_bar = None
            return current_bar
        
        def process_strat_data(original_strat_data, check_for_future_data):
            sufficient_data = True
            
            if isinstance(original_strat_data, dict):
                if 'aux' in original_strat_data:
                    base_data = original_strat_data['base']
                    processed_auxdata = self._check_auxdata(original_strat_data['aux'],
                                    timestamp, indexing, bars, check_for_future_data)
                else:
                    # MTF data
                    base_data = original_strat_data
                
                # Process base OHLC data
                processed_basedata = {}
                for granularity, data in base_data.items():
                    processed_basedata[granularity] = self._check_ohlc_data(data, 
                                timestamp, indexing, bars, check_for_future_data)
                
                # Combine the results of the conditionals above
                strat_data = {}
                if 'aux' in original_strat_data:
                    strat_data['aux'] = processed_auxdata
                    strat_data['base'] = processed_basedata
                else:
                    strat_data = processed_basedata
                    
                # Extract current bar
                first_tf_data = processed_basedata[list(processed_basedata.keys())[0]]
                current_bar = get_current_bar(first_tf_data)
                
                # Check that enough bars have accumulated
                if len(first_tf_data) < bars:
                    sufficient_data = False
                
            elif isinstance(original_strat_data, pd.DataFrame):
                strat_data = self._check_ohlc_data(original_strat_data, 
                             timestamp, indexing, bars, check_for_future_data)
                current_bar = get_current_bar(strat_data)
                
                # Check that enough bars have accumulated
                if len(strat_data) < bars:
                    sufficient_data = False
            
            else:
                raise Exception("Unrecognised data type. Cannot process.")
            
            return strat_data, current_bar, sufficient_data
        
        bars = self._strategy_params['period']
        
        if self._backtest_mode:
            check_for_future_data = True
        else:
            self._refresh_data()
            check_for_future_data = False

        strat_data, current_bar, sufficient_data = process_strat_data(self._strat_data, 
                                                                      check_for_future_data)

        # Process quote data
        quote_data = self._check_ohlc_data(self.quote_data, timestamp, 
                                           indexing, bars)
        quote_bar = get_current_bar(quote_data)
        
        return strat_data, current_bar, quote_bar, sufficient_data
    
    
    def _check_last_bar(self, current_bar) -> bool:
        """Checks for duplicate data to prevent duplicate signals.
        """
        duplicate = False
        if self._run_mode == 'continuous':
            # For now, will just check current_bar doesn't match last bar
            # For extension, can check that the bar isn't too close to the previous,
            # in the case of MTF or other
            if self._last_bar is not None:
                duplicate = (current_bar == self._last_bar).all()
            
            # Reset last bar
            self._last_bar = current_bar
            
        if int(self._verbosity) > 1 and duplicate:
            print("Duplicate bar detected. Skipping.")
        
        return duplicate
        
        