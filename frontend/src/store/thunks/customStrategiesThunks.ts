import { createAsyncThunk } from '@reduxjs/toolkit';
import { api, ApiError } from '../../api';
import type { CustomStrategyRules } from '../../types/customStrategy';

// Redux Toolkit serializes a thrown Error down to {name, message, stack}
// before it reaches `.rejected`/`.unwrap()` — that would silently drop
// ApiError's `.detail` (rule_schema.validate_rules()'s per-line error
// list). rejectWithValue bypasses that serialization: whatever's passed
// to it becomes `action.payload` / what `.unwrap()` throws, verbatim.
function detailOrMessage(err: unknown, fallback: string): string | string[] {
  if (err instanceof ApiError && err.detail) return err.detail;
  return err instanceof Error ? err.message : fallback;
}

export const fetchCustomStrategies = createAsyncThunk(
  'customStrategies/fetchAll',
  async () => {
    return await api.listCustomStrategies();
  }
);

export const createCustomStrategy = createAsyncThunk(
  'customStrategies/create',
  async (payload: { name: string; instrument_type: string; symbols: string[]; rules: CustomStrategyRules }, { rejectWithValue }) => {
    try {
      return await api.createCustomStrategy(payload);
    } catch (err) {
      return rejectWithValue(detailOrMessage(err, 'Failed to create strategy'));
    }
  }
);

export const updateCustomStrategy = createAsyncThunk(
  'customStrategies/update',
  async ({ id, payload }: { id: number; payload: { name: string; symbols: string[]; rules: CustomStrategyRules } }, { rejectWithValue }) => {
    try {
      return await api.updateCustomStrategy(id, payload);
    } catch (err) {
      return rejectWithValue(detailOrMessage(err, 'Failed to update strategy'));
    }
  }
);

export const updateCustomStrategyStatus = createAsyncThunk(
  'customStrategies/updateStatus',
  async ({ id, status }: { id: number; status: string }) => {
    return await api.updateCustomStrategyStatus(id, status);
  }
);

export const deleteCustomStrategy = createAsyncThunk(
  'customStrategies/delete',
  async (id: number) => {
    await api.deleteCustomStrategy(id);
    return id;
  }
);
