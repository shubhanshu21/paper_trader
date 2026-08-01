import { createAsyncThunk } from '@reduxjs/toolkit';
import { api, OrderLeg } from '../../api';

export const fetchAdvancedOrders = createAsyncThunk(
  'advancedOrders/fetchAll',
  async () => {
    return await api.getAdvancedOrders();
  }
);

export const createOcoOrder = createAsyncThunk(
  'advancedOrders/createOco',
  async (req: { mode: 'paper' | 'live'; primary_order: OrderLeg; secondary_order: OrderLeg; strategy_name?: string }, { dispatch }) => {
    const result = await api.createOcoOrder(req);
    await dispatch(fetchAdvancedOrders());
    return result;
  }
);

export const cancelOcoOrder = createAsyncThunk(
  'advancedOrders/cancelOco',
  async (ocoId: string, { dispatch }) => {
    await api.cancelOcoOrder(ocoId);
    await dispatch(fetchAdvancedOrders());
    return ocoId;
  }
);

export const createTrailingStop = createAsyncThunk(
  'advancedOrders/createTrailingStop',
  async (req: {
    mode: 'paper' | 'live'; instrument_token: string; symbol: string; side: 'BUY' | 'SELL';
    quantity: number; trail_amount: number; trail_type: 'points' | 'percentage'; product?: string; strategy_name?: string;
  }, { dispatch }) => {
    const result = await api.createTrailingStop(req);
    await dispatch(fetchAdvancedOrders());
    return result;
  }
);

export const cancelTrailingStop = createAsyncThunk(
  'advancedOrders/cancelTrailingStop',
  async (tsId: string, { dispatch }) => {
    await api.cancelTrailingStop(tsId);
    await dispatch(fetchAdvancedOrders());
    return tsId;
  }
);

export const createBracketOrder = createAsyncThunk(
  'advancedOrders/createBracket',
  async (req: { mode: 'paper' | 'live'; entry_order: OrderLeg; take_profit: OrderLeg; stop_loss: OrderLeg; strategy_name?: string }, { dispatch }) => {
    const result = await api.createBracketOrder(req);
    await dispatch(fetchAdvancedOrders());
    return result;
  }
);

export const cancelBracketOrder = createAsyncThunk(
  'advancedOrders/cancelBracket',
  async (bracketId: string, { dispatch }) => {
    await api.cancelBracketOrder(bracketId);
    await dispatch(fetchAdvancedOrders());
    return bracketId;
  }
);
