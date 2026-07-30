import { createSlice } from '@reduxjs/toolkit';
import { WalletSummary, Order, OptionsPosition, EquityPosition, LedgerItem } from '../../api';
import {
  fetchWalletSummary,
  fetchWalletLedger,
  fetchOrders,
  fetchOpenOptions,
  fetchClosedOptions,
  fetchOpenEquity,
  fetchClosedEquity,
  closeOptionPosition,
  closeEquityPosition,
  adjustWallet,
  setStartingCapital,
  resetWallet,
} from '../thunks/dataThunks';

interface DataState {
  wallet: WalletSummary | null;
  ledger: LedgerItem[];
  orders: Order[];
  openOptions: OptionsPosition[];
  closedOptions: OptionsPosition[];
  openEquity: EquityPosition[];
  closedEquity: EquityPosition[];
  ltps: { [key: string]: number };
  closingId: number | null;
  loading: {
    wallet: boolean;
    ledger: boolean;
    orders: boolean;
    options: boolean;
    equity: boolean;
  };
  error: string | null;
}

const initialState: DataState = {
  wallet: null,
  ledger: [],
  orders: [],
  openOptions: [],
  closedOptions: [],
  openEquity: [],
  closedEquity: [],
  ltps: {},
  closingId: null,
  loading: {
    wallet: false,
    ledger: false,
    orders: false,
    options: false,
    equity: false,
  },
  error: null,
};

const dataSlice = createSlice({
  name: 'data',
  initialState,
  reducers: {
    updateLtps: (state, action) => {
      state.ltps = { ...state.ltps, ...action.payload };
    },
    // Fed by the /ws/positions socket (App.tsx) every ~3s — replaces the
    // one-shot REST snapshot with live MTM/current_price so Dashboard,
    // Positions, and Holdings all reflect real-time P&L without polling.
    setLivePositions: (state, action: { payload: { options: OptionsPosition[]; equity: EquityPosition[] } }) => {
      state.openOptions = action.payload.options;
      state.openEquity = action.payload.equity;
      const ltpUpdates: { [key: string]: number } = {};
      for (const pos of action.payload.equity) {
        if (pos.current_price != null) ltpUpdates[pos.symbol] = pos.current_price;
      }
      state.ltps = { ...state.ltps, ...ltpUpdates };
    },
    clearError: (state) => {
      state.error = null;
    },
  },
  extraReducers: (builder) => {
    // Wallet Summary
    builder
      .addCase(fetchWalletSummary.pending, (state) => {
        state.loading.wallet = true;
        state.error = null;
      })
      .addCase(fetchWalletSummary.fulfilled, (state, action) => {
        state.loading.wallet = false;
        state.wallet = action.payload;
      })
      .addCase(fetchWalletSummary.rejected, (state, action) => {
        state.loading.wallet = false;
        state.error = action.error.message || 'Failed to fetch wallet summary';
      });

    // Wallet Ledger
    builder
      .addCase(fetchWalletLedger.pending, (state) => {
        state.loading.ledger = true;
        state.error = null;
      })
      .addCase(fetchWalletLedger.fulfilled, (state, action) => {
        state.loading.ledger = false;
        state.ledger = action.payload;
      })
      .addCase(fetchWalletLedger.rejected, (state, action) => {
        state.loading.ledger = false;
        state.error = action.error.message || 'Failed to fetch wallet ledger';
      });

    // Orders
    builder
      .addCase(fetchOrders.pending, (state) => {
        state.loading.orders = true;
        state.error = null;
      })
      .addCase(fetchOrders.fulfilled, (state, action) => {
        state.loading.orders = false;
        state.orders = action.payload;
      })
      .addCase(fetchOrders.rejected, (state, action) => {
        state.loading.orders = false;
        state.error = action.error.message || 'Failed to fetch orders';
      });

    // Open Options
    builder
      .addCase(fetchOpenOptions.pending, (state) => {
        state.loading.options = true;
        state.error = null;
      })
      .addCase(fetchOpenOptions.fulfilled, (state, action) => {
        state.loading.options = false;
        state.openOptions = action.payload;
      })
      .addCase(fetchOpenOptions.rejected, (state, action) => {
        state.loading.options = false;
        state.error = action.error.message || 'Failed to fetch open options';
      });

    // Closed Options
    builder
      .addCase(fetchClosedOptions.fulfilled, (state, action) => {
        state.closedOptions = action.payload as OptionsPosition[];
      });

    // Open Equity
    builder
      .addCase(fetchOpenEquity.pending, (state) => {
        state.loading.equity = true;
        state.error = null;
      })
      .addCase(fetchOpenEquity.fulfilled, (state, action) => {
        state.loading.equity = false;
        state.openEquity = action.payload;
      })
      .addCase(fetchOpenEquity.rejected, (state, action) => {
        state.loading.equity = false;
        state.error = action.error.message || 'Failed to fetch open equity';
      });

    // Closed Equity
    builder
      .addCase(fetchClosedEquity.fulfilled, (state, action) => {
        state.closedEquity = action.payload as EquityPosition[];
      });

    // Close Option Position
    builder
      .addCase(closeOptionPosition.pending, (state, action) => {
        state.loading.options = true;
        state.closingId = action.meta.arg;
      })
      .addCase(closeOptionPosition.fulfilled, (state, action) => {
        state.loading.options = false;
        state.closingId = null;
        state.openOptions = state.openOptions.filter(pos => pos.id !== action.payload);
      })
      .addCase(closeOptionPosition.rejected, (state, action) => {
        state.loading.options = false;
        state.closingId = null;
        state.error = action.error.message || 'Failed to close position';
      });

    // Close Equity Position
    builder
      .addCase(closeEquityPosition.pending, (state, action) => {
        state.loading.equity = true;
        state.closingId = action.meta.arg;
      })
      .addCase(closeEquityPosition.fulfilled, (state, action) => {
        state.loading.equity = false;
        state.closingId = null;
        state.openEquity = state.openEquity.filter(pos => pos.id !== action.payload);
      })
      .addCase(closeEquityPosition.rejected, (state, action) => {
        state.loading.equity = false;
        state.closingId = null;
        state.error = action.error.message || 'Failed to close position';
      });

    // Adjust Wallet
    builder
      .addCase(adjustWallet.fulfilled, (state, action) => {
        state.wallet = action.payload;
      });

    // Set Starting Capital
    builder
      .addCase(setStartingCapital.fulfilled, (state, action) => {
        state.wallet = action.payload;
      });

    // Reset Wallet
    builder
      .addCase(resetWallet.fulfilled, (state) => {
        state.wallet = null;
        state.ledger = [];
      });
  },
});

export const { updateLtps, setLivePositions, clearError } = dataSlice.actions;
export default dataSlice.reducer;
