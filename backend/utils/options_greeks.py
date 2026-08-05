"""
utils/options_greeks.py — Options Greeks calculation utilities.

Provides Black-Scholes options pricing and Greeks calculation
for real-time options analytics and risk management.
"""
import math

try:
    import mibian
    MIBIAN_AVAILABLE = True
except ImportError:
    MIBIAN_AVAILABLE = False


class OptionsGreeks:
    """Calculate options Greeks using Black-Scholes model."""
    
    @staticmethod
    def calculate_greeks(
        spot_price: float,
        strike_price: float,
        time_to_expiry: float,  # in years
        risk_free_rate: float,  # annual rate
        volatility: float,  # annual volatility
        option_type: str = 'call'  # 'call' or 'put'
    ) -> dict:
        """
        Calculate option Greeks using Black-Scholes model.
        
        Returns dict with delta, gamma, theta, vega, rho, and premium.
        """
        if not MIBIAN_AVAILABLE:
            # Fallback to simplified Black-Scholes if mibian not available
            return OptionsGreeks._black_scholes_fallback(
                spot_price, strike_price, time_to_expiry,
                risk_free_rate, volatility, option_type
            )
        
        try:
            # Convert time to expiry to days for mibian
            days_to_expiry = int(time_to_expiry * 365)
            
            if option_type.lower() == 'call':
                bs = mibian.BS(
                    [spot_price, strike_price, risk_free_rate, days_to_expiry],
                    volatility=volatility
                )
                return {
                    'delta': bs.callDelta,
                    'gamma': bs.gamma,
                    'theta': bs.callTheta,
                    'vega': bs.vega,
                    'rho': bs.callRho,
                    'premium': bs.callPrice,
                    'implied_volatility': volatility
                }
            else:
                bs = mibian.BS(
                    [spot_price, strike_price, risk_free_rate, days_to_expiry],
                    volatility=volatility
                )
                return {
                    'delta': bs.putDelta,
                    'gamma': bs.gamma,
                    'theta': bs.putTheta,
                    'vega': bs.vega,
                    'rho': bs.putRho,
                    'premium': bs.putPrice,
                    'implied_volatility': volatility
                }
        except Exception:
            # Fallback on error
            return OptionsGreeks._black_scholes_fallback(
                spot_price, strike_price, time_to_expiry,
                risk_free_rate, volatility, option_type
            )
    
    @staticmethod
    def _black_scholes_fallback(
        spot_price: float,
        strike_price: float,
        time_to_expiry: float,
        risk_free_rate: float,
        volatility: float,
        option_type: str = 'call'
    ) -> dict:
        """Fallback Black-Scholes calculation using pure Python."""
        
        d1 = (math.log(spot_price / strike_price) + 
              (risk_free_rate + 0.5 * volatility ** 2) * time_to_expiry) / \
              (volatility * math.sqrt(time_to_expiry))
        
        d2 = d1 - volatility * math.sqrt(time_to_expiry)
        
        # Calculate N(d1) and N(d2) using approximation
        def normal_cdf(x):
            return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0
        
        def normal_pdf(x):
            return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)
        
        nd1 = normal_cdf(d1)
        nd2 = normal_cdf(d2)
        nd1_minus = normal_cdf(-d1)
        nd2_minus = normal_cdf(-d2)
        
        if option_type.lower() == 'call':
            premium = spot_price * nd1 - strike_price * math.exp(-risk_free_rate * time_to_expiry) * nd2
            delta = nd1
        else:
            premium = strike_price * math.exp(-risk_free_rate * time_to_expiry) * nd2_minus - spot_price * nd1_minus
            delta = nd1 - 1
        
        gamma = normal_pdf(d1) / (spot_price * volatility * math.sqrt(time_to_expiry))
        
        theta = (-spot_price * normal_pdf(d1) * volatility / (2 * math.sqrt(time_to_expiry)) -
                 risk_free_rate * strike_price * math.exp(-risk_free_rate * time_to_expiry) * nd2) / 365
        
        vega = spot_price * normal_pdf(d1) * math.sqrt(time_to_expiry) / 100
        
        rho = (strike_price * time_to_expiry * math.exp(-risk_free_rate * time_to_expiry) * nd2) / 100 if option_type == 'call' else \
              (-strike_price * time_to_expiry * math.exp(-risk_free_rate * time_to_expiry) * nd2_minus) / 100
        
        return {
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'vega': vega,
            'rho': rho,
            'premium': premium,
            'implied_volatility': volatility
        }
    
    @staticmethod
    def calculate_portfolio_greeks(positions: list) -> dict:
        """
        Calculate portfolio-level Greeks by summing individual position Greeks.
        
        Each position should be a dict with:
        - quantity: int (positive for long, negative for short)
        - greeks: dict with delta, gamma, theta, vega, rho
        """
        portfolio_greeks = {
            'delta': 0.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0,
            'rho': 0.0
        }
        
        for position in positions:
            qty = position.get('quantity', 0)
            greeks = position.get('greeks', {})
            
            portfolio_greeks['delta'] += qty * greeks.get('delta', 0)
            portfolio_greeks['gamma'] += qty * greeks.get('gamma', 0)
            portfolio_greeks['theta'] += qty * greeks.get('theta', 0)
            portfolio_greeks['vega'] += qty * greeks.get('vega', 0)
            portfolio_greeks['rho'] += qty * greeks.get('rho', 0)
        
        return portfolio_greeks
