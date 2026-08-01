import { createSlice } from '@reduxjs/toolkit';
import { OcoOrder, TrailingStopOrder, BracketOrder } from '../../api';
import {
  fetchAdvancedOrders,
  createOcoOrder,
  cancelOcoOrder,
  createTrailingStop,
  cancelTrailingStop,
  createBracketOrder,
  cancelBracketOrder,
} from '../thunks/advancedOrdersThunks';

interface AdvancedOrdersState {
  ocoOrders: OcoOrder[];
  trailingStops: TrailingStopOrder[];
  bracketOrders: BracketOrder[];
  loading: boolean;
  creating: boolean;
  cancellingId: string | null;
  error: string | null;
}

const initialState: AdvancedOrdersState = {
  ocoOrders: [],
  trailingStops: [],
  bracketOrders: [],
  loading: false,
  creating: false,
  cancellingId: null,
  error: null,
};

const advancedOrdersSlice = createSlice({
  name: 'advancedOrders',
  initialState,
  reducers: {
    clearAdvancedOrdersError: (state) => {
      state.error = null;
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchAdvancedOrders.pending, (state) => {
        state.loading = true;
        state.error = null;
      })
      .addCase(fetchAdvancedOrders.fulfilled, (state, action) => {
        state.loading = false;
        state.ocoOrders = action.payload.oco_orders;
        state.trailingStops = action.payload.trailing_stops;
        state.bracketOrders = action.payload.bracket_orders;
      })
      .addCase(fetchAdvancedOrders.rejected, (state, action) => {
        state.loading = false;
        state.error = action.error.message || 'Failed to fetch advanced orders';
      });

    for (const createThunk of [createOcoOrder, createTrailingStop, createBracketOrder]) {
      builder
        .addCase(createThunk.pending, (state) => {
          state.creating = true;
          state.error = null;
        })
        .addCase(createThunk.fulfilled, (state) => {
          state.creating = false;
        })
        .addCase(createThunk.rejected, (state, action) => {
          state.creating = false;
          state.error = action.error.message || 'Failed to create order';
        });
    }

    for (const cancelThunk of [cancelOcoOrder, cancelTrailingStop, cancelBracketOrder]) {
      builder
        .addCase(cancelThunk.pending, (state, action) => {
          state.cancellingId = action.meta.arg;
        })
        .addCase(cancelThunk.fulfilled, (state) => {
          state.cancellingId = null;
        })
        .addCase(cancelThunk.rejected, (state, action) => {
          state.cancellingId = null;
          state.error = action.error.message || 'Failed to cancel order';
        });
    }
  },
});

export const { clearAdvancedOrdersError } = advancedOrdersSlice.actions;
export default advancedOrdersSlice.reducer;
