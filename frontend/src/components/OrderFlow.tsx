import { useState, useEffect } from 'react';
import { ArrowUp, ArrowDown } from 'lucide-react';
import { C, FONT } from './Common';

interface DOMLevel {
  price: number;
  bidSize: number;
  askSize: number;
  totalBid: number;
  totalAsk: number;
}

interface Trade {
  time: string;
  price: number;
  size: number;
  side: 'buy' | 'sell';
}

interface OrderFlowProps {
  symbol: string;
}

export default function OrderFlow({ symbol }: OrderFlowProps) {
  const [domData, setDomData] = useState<DOMLevel[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [activeTab, setActiveTab] = useState<'dom' | 'trades'>('dom');

  // Mock data generation (in production, this would come from WebSocket)
  useEffect(() => {
    const generateDOMData = () => {
      const basePrice = 18000;
      const levels: DOMLevel[] = [];
      
      for (let i = 0; i < 10; i++) {
        const price = basePrice + (5 - i) * 0.5;
        levels.push({
          price,
          bidSize: Math.floor(Math.random() * 1000) + 100,
          askSize: Math.floor(Math.random() * 1000) + 100,
          totalBid: 0,
          totalAsk: 0,
        });
      }
      
      // Calculate cumulative totals
      let totalBid = 0;
      let totalAsk = 0;
      for (let i = levels.length - 1; i >= 0; i--) {
        totalBid += levels[i].bidSize;
        totalAsk += levels[i].askSize;
        levels[i].totalBid = totalBid;
        levels[i].totalAsk = totalAsk;
      }
      
      return levels;
    };

    const generateTrades = () => {
      const newTrades: Trade[] = [];
      const basePrice = 18000;
      
      for (let i = 0; i < 20; i++) {
        newTrades.push({
          time: `${String(Math.floor(Math.random() * 24)).padStart(2, '0')}:${String(Math.floor(Math.random() * 60)).padStart(2, '0')}:${String(Math.floor(Math.random() * 60)).padStart(2, '0')}`,
          price: basePrice + (Math.random() - 0.5) * 2,
          size: Math.floor(Math.random() * 500) + 50,
          side: Math.random() > 0.5 ? 'buy' : 'sell',
        });
      }
      
      return newTrades.sort((a, b) => b.time.localeCompare(a.time));
    };

    setDomData(generateDOMData());
    setTrades(generateTrades());

    // Simulate real-time updates
    const interval = setInterval(() => {
      setDomData(generateDOMData());
      setTrades(generateTrades());
    }, 2000);

    return () => clearInterval(interval);
  }, [symbol]);

  const maxTotal = Math.max(
    ...domData.map(d => Math.max(d.totalBid, d.totalAsk)),
    1
  );

  return (
    <div className="w-full bg-white border rounded-lg overflow-hidden" style={FONT}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: C.border2 }}>
        <h3 className="text-sm font-semibold text-gray-700">Order Flow - {symbol}</h3>
        <div className="flex gap-2">
          <button
            onClick={() => setActiveTab('dom')}
            className={`px-3 py-1 text-xs font-medium rounded ${
              activeTab === 'dom' ? 'bg-orange-500 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            DOM
          </button>
          <button
            onClick={() => setActiveTab('trades')}
            className={`px-3 py-1 text-xs font-medium rounded ${
              activeTab === 'trades' ? 'bg-orange-500 text-white' : 'bg-gray-100 text-gray-600'
            }`}
          >
            Time & Sales
          </button>
        </div>
      </div>

      {activeTab === 'dom' ? (
        /* DOM Ladder */
        <div className="p-4">
          <div className="grid grid-cols-4 gap-2 text-xs font-semibold text-gray-500 mb-2">
            <div>Bid Size</div>
            <div className="text-right">Price</div>
            <div className="text-right">Price</div>
            <div className="text-right">Ask Size</div>
          </div>
          
          {domData.map((level, idx) => (
            <div key={idx} className="grid grid-cols-4 gap-2 items-center py-1 border-b" style={{ borderColor: C.border }}>
              {/* Bid side */}
              <div className="relative">
                <div className="text-xs font-mono text-green-600">{level.bidSize}</div>
                <div
                  className="absolute top-0 left-0 h-full bg-green-100"
                  style={{
                    width: `${(level.totalBid / maxTotal) * 100}%`,
                    opacity: 0.3,
                  }}
                />
              </div>
              
              {/* Bid price */}
              <div className="text-xs font-mono text-green-600 text-right">{level.price.toFixed(2)}</div>
              
              {/* Ask price */}
              <div className="text-xs font-mono text-red-600 text-right">{level.price.toFixed(2)}</div>
              
              {/* Ask side */}
              <div className="relative">
                <div className="text-xs font-mono text-red-600 text-right">{level.askSize}</div>
                <div
                  className="absolute top-0 right-0 h-full bg-red-100"
                  style={{
                    width: `${(level.totalAsk / maxTotal) * 100}%`,
                    opacity: 0.3,
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      ) : (
        /* Time & Sales */
        <div className="p-4">
          <div className="grid grid-cols-4 gap-2 text-xs font-semibold text-gray-500 mb-2">
            <div>Time</div>
            <div>Price</div>
            <div>Size</div>
            <div>Side</div>
          </div>
          
          <div className="max-h-96 overflow-y-auto">
            {trades.map((trade, idx) => (
              <div key={idx} className="grid grid-cols-4 gap-2 items-center py-1 border-b" style={{ borderColor: C.border }}>
                <div className="text-xs font-mono text-gray-500">{trade.time}</div>
                <div className="text-xs font-mono text-gray-700">{trade.price.toFixed(2)}</div>
                <div className="text-xs font-mono text-gray-700">{trade.size}</div>
                <div className="flex items-center gap-1">
                  {trade.side === 'buy' ? (
                    <ArrowUp size={12} className="text-green-600" />
                  ) : (
                    <ArrowDown size={12} className="text-red-600" />
                  )}
                  <span className={`text-xs font-medium ${trade.side === 'buy' ? 'text-green-600' : 'text-red-600'}`}>
                    {trade.side.toUpperCase()}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
