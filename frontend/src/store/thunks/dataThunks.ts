import { createAsyncThunk } from '@reduxjs/toolkit';
import { api } from '../../api';

// Thunks for fetching data from backend

export const fetchWalletSummary = createAsyncThunk(
  'data/fetchWalletSummary',
  async () => {
    return await api.getWalletSummary();
  }
);

export const fetchWalletLedger = createAsyncThunk(
  'data/fetchWalletLedger',
  async () => {
    return await api.getWalletLedger();
  }
);

export const fetchOrders = createAsyncThunk(
  'data/fetchOrders',
  async (mode?: string) => {
    return await api.getOrders(mode);
  }
);

export const fetchOpenOptions = createAsyncThunk(
  'data/fetchOpenOptions',
  async () => {
    return await api.getOpenOptions();
  }
);

export const fetchClosedOptions = createAsyncThunk(
  'data/fetchClosedOptions',
  async (limit: number = 50) => {
    return await api.getClosedOptions(limit);
  }
);

export const fetchOpenEquity = createAsyncThunk(
  'data/fetchOpenEquity',
  async () => {
    return await api.getOpenEquity();
  }
);

export const fetchClosedEquity = createAsyncThunk(
  'data/fetchClosedEquity',
  async (limit: number = 100) => {
    return await api.getClosedEquity(limit);
  }
);

export const closeOptionPosition = createAsyncThunk(
  'data/closeOptionPosition',
  async (id: number) => {
    await api.closeOption(id);
    return id;
  }
);

export const closeEquityPosition = createAsyncThunk(
  'data/closeEquityPosition',
  async (id: number) => {
    await api.closeEquity(id);
    return id;
  }
);

export const adjustWallet = createAsyncThunk(
  'data/adjustWallet',
  async ({ amount, note }: { amount: number; note: string }) => {
    return await api.adjustWallet(amount, note);
  }
);

export const setStartingCapital = createAsyncThunk(
  'data/setStartingCapital',
  async (starting_capital: number) => {
    return await api.setStartingCapital(starting_capital);
  }
);

export const resetWallet = createAsyncThunk(
  'data/resetWallet',
  async () => {
    return await api.resetWallet();
  }
);
