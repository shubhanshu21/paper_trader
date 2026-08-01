import { createSlice } from '@reduxjs/toolkit';
import { CustomStrategy } from '../../api';
import {
  fetchCustomStrategies, createCustomStrategy, updateCustomStrategy,
  updateCustomStrategyStatus, deleteCustomStrategy,
} from '../thunks/customStrategiesThunks';

interface CustomStrategiesState {
  strategies: CustomStrategy[];
  loading: boolean;
  creating: boolean;
  updatingId: number | null;
  statusUpdatingId: number | null;
  deletingId: number | null;
  error: string | null;
}

const initialState: CustomStrategiesState = {
  strategies: [],
  loading: false,
  creating: false,
  updatingId: null,
  statusUpdatingId: null,
  deletingId: null,
  error: null,
};

const customStrategiesSlice = createSlice({
  name: 'customStrategies',
  initialState,
  reducers: {
    clearCustomStrategiesError: (state) => {
      state.error = null;
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchCustomStrategies.pending, (state) => {
        state.loading = true;
        state.error = null;
      })
      .addCase(fetchCustomStrategies.fulfilled, (state, action) => {
        state.loading = false;
        state.strategies = action.payload.strategies;
      })
      .addCase(fetchCustomStrategies.rejected, (state, action) => {
        state.loading = false;
        state.error = action.error.message || 'Failed to fetch strategies';
      })

      .addCase(createCustomStrategy.pending, (state) => {
        state.creating = true;
        state.error = null;
      })
      .addCase(createCustomStrategy.fulfilled, (state, action) => {
        state.creating = false;
        state.strategies = [action.payload, ...state.strategies];
      })
      .addCase(createCustomStrategy.rejected, (state, action) => {
        state.creating = false;
        const detail = action.payload as string | string[] | undefined;
        state.error = (Array.isArray(detail) ? detail.join(' ') : detail) || action.error.message || 'Failed to create strategy';
      })

      .addCase(updateCustomStrategy.pending, (state, action) => {
        state.updatingId = action.meta.arg.id;
        state.error = null;
      })
      .addCase(updateCustomStrategy.fulfilled, (state, action) => {
        state.updatingId = null;
        state.strategies = state.strategies.map((s) => (s.id === action.payload.id ? action.payload : s));
      })
      .addCase(updateCustomStrategy.rejected, (state, action) => {
        state.updatingId = null;
        const detail = action.payload as string | string[] | undefined;
        state.error = (Array.isArray(detail) ? detail.join(' ') : detail) || action.error.message || 'Failed to update strategy';
      })

      .addCase(updateCustomStrategyStatus.pending, (state, action) => {
        state.statusUpdatingId = action.meta.arg.id;
        state.error = null;
      })
      .addCase(updateCustomStrategyStatus.fulfilled, (state, action) => {
        state.statusUpdatingId = null;
        state.strategies = state.strategies.map((s) => (s.id === action.payload.id ? action.payload : s));
      })
      .addCase(updateCustomStrategyStatus.rejected, (state, action) => {
        state.statusUpdatingId = null;
        state.error = action.error.message || 'Failed to update strategy status';
      })

      .addCase(deleteCustomStrategy.pending, (state, action) => {
        state.deletingId = action.meta.arg;
        state.error = null;
      })
      .addCase(deleteCustomStrategy.fulfilled, (state, action) => {
        state.deletingId = null;
        state.strategies = state.strategies.filter((s) => s.id !== action.payload);
      })
      .addCase(deleteCustomStrategy.rejected, (state, action) => {
        state.deletingId = null;
        state.error = action.error.message || 'Failed to delete strategy';
      });
  },
});

export const { clearCustomStrategiesError } = customStrategiesSlice.actions;
export default customStrategiesSlice.reducer;
