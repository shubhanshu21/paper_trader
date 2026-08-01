import { configureStore } from '@reduxjs/toolkit';
import authReducer from './slices/authSlice';
import uiReducer from './slices/uiSlice';
import dataReducer from './slices/dataSlice';
import advancedOrdersReducer from './slices/advancedOrdersSlice';
import customStrategiesReducer from './slices/customStrategiesSlice';

export const store = configureStore({
  reducer: {
    auth: authReducer,
    ui: uiReducer,
    data: dataReducer,
    advancedOrders: advancedOrdersReducer,
    customStrategies: customStrategiesReducer,
  },
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
