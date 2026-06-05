"""
One-shot script: download S&P 500 + S&P 400 + NASDAQ 100 + curated extras
and write data/us_universe.json.  Run manually whenever you want to refresh.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT  = ROOT / "data" / "us_universe.json"

def _fetch_wiki_tickers(url: str, col: str) -> list[str]:
    try:
        import pandas as pd
        import urllib.request
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ticker-fetcher/1.0)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
        import io
        tables = pd.read_html(io.StringIO(html))
        for t in tables:
            if col in t.columns:
                return t[col].dropna().tolist()
    except Exception as e:
        print(f"  Warning: could not fetch {url}: {e}")
    return []

def main():
    tickers: set[str] = set()

    print("Fetching S&P 500...")
    tickers.update(_fetch_wiki_tickers(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"))

    print("Fetching S&P 400 mid-cap...")
    tickers.update(_fetch_wiki_tickers(
        "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "Symbol"))

    print("Fetching NASDAQ-100...")
    tickers.update(_fetch_wiki_tickers(
        "https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker"))

    # Curated high-momentum / high-volume extras not always in indexes
    extras = [
        # Mega-cap tech
        "AAPL","MSFT","NVDA","AMD","META","GOOGL","GOOG","AMZN","TSLA",
        # Semis
        "AVGO","QCOM","MU","MRVL","AMAT","LRCX","KLAC","INTC","TXN",
        "ON","MCHP","WOLF","SMCI","ARM","SLAB","MPWR","NXPI","SWKS","QRVO",
        # Cloud / SaaS
        "CRM","NOW","SNOW","PLTR","ORCL","ADBE","WDAY","TEAM","DDOG","NET",
        "ZS","PANW","CRWD","FTNT","CYBR","S","OKTA","HUBS","MDB","CFLT",
        "GTLB","APP","APPF","BILL","BRZE","ZI","DT","ESTC","FROG","PD",
        # Fintech / crypto
        "V","MA","PYPL","SQ","COIN","HOOD","SOFI","AFRM","NU","UPST",
        "MARA","RIOT","CLSK","HUT","CIFR","BTBT","IREN",
        # Banks / finance
        "JPM","BAC","GS","MS","C","WFC","BX","BLK","SCHW","IBKR","RJF",
        "AXP","COF","DFS","SYF","ALLY","LC","OMF",
        # Energy
        "XOM","CVX","COP","OXY","MRO","DVN","FANG","HES","EOG",
        "SLB","HAL","BKR","NOG","SM","AR","CTRA","RRC","EQT","CHRD",
        # Healthcare / biotech
        "LLY","NVO","ABBV","MRK","PFE","BMY","AMGN","GILD","REGN","VRTX",
        "MRNA","BNTX","BIIB","ALNY","EXAS","RXRX","ACAD","ARWR","BEAM",
        "CRSP","EDIT","NTLA","FATE","KYMR","PRGO","JAZZ","INCY","SGEN",
        "RVMD","KRYS","CGON","DNLI","IMVT","NVCR","TGTX","ZNTL",
        # Consumer / retail
        "WMT","COST","TGT","HD","LOW","NKE","DIS","NFLX","UBER","LYFT",
        "DASH","BKNG","ABNB","EXPE","TRIP","YELP","GRUB","CART","DKNG",
        "PENN","CZR","MGM","LVS","WYNN","RCL","CCL","NCLH","DAL","UAL","AAL","LUV",
        # AI / quantum / space
        "DELL","HPE","IONQ","RGTI","QUBT","LUNR","RKLB","SPCE","ASTS",
        "BBAI","SOUN","AISP","GFAI","BTAI","NLSP",
        # EVs / clean energy
        "RIVN","LCID","NIO","LI","XPEV","ENPH","FSLR","RUN","NOVA",
        "F","GM","PLUG","BE","CHPT","BLNK","EVGO","PTRA",
        # Industrials / defense
        "GE","CAT","BA","RTX","LMT","NOC","DE","HON","GD","HII",
        "LHX","LDOS","SAIC","CACI","KTOS","AVAV","RDW",
        # Media / streaming / gaming
        "RBLX","EA","TTWO","SPOT","PARA","WBD","FOX","FOXA","NWSA",
        # Meme / high short interest
        "GME","AMC","BBBY","CLOV","SNDL","NKLA","WKHS","RIDE","HYLN",
        # Small/mid cap momentum
        "SMTC","POWI","SITM","ALGM","AEHR","FORM","UCTT","ACMR",
        "LSCC","AMBA","PLAB","COHU","ONTO","ICHR","AEIS","MKSI",
        "DIOD","IXYS","CRUS","RMBS","PDFS","AXTI","MTSI","AOSL",
        # REITs
        "O","AMT","PLD","WELL","SPG","EQR","AVB","PSA","EXR","DLR",
        "IRM","SBAC","CCI","VICI","GLPI","STAG","COLD","IIPR",
        # Popular under $10 for PM/AH plays
        "BBAI","SOUN","OPEN","ACHR","PLUG","MARA","RIOT","CLSK","HOOD",
        "SOFI","CLOV","SPCE","WKHS","NKLA","SNDL","NIO","XPEV","LCID",
        "RIVN","IONQ","RGTI","QUBT","LUNR","ASTS","RKLB","BTBT","IREN",
        "CIFR","HUT","BBBY","NWL","OPK","KEEL","ACMR","BKSY","MULN",
        "VINC","INDO","SMFL","PHIO","SHOT","IDT","EVTL","JOBY","LILM",
        "ARCHER","VOXX","EZFL","COVA","PSHG","GXII","AEYE","MNMD",
        # Additional S&P 500 / high-liquidity
        "AAON","ACM","AES","AIG","AIZ","AJG","AKAM","ALB","ALGN","ALK",
        "ALL","ALLE","ANET","AON","APA","APD","APH","APTV","ARE","ATO",
        "AWK","AZO","BBY","BDX","BEN","BIO","BK","BMRN","BR","BRK-B",
        "BRO","BSX","BXP","CB","CBOE","CBRE","CDW","CE","CF","CHD",
        "CHRW","CHTR","CI","CINF","CLX","CMCSA","CMS","CNC","CNP",
        "COO","CPRT","CPT","CSX","CTAS","CTLT","CTSH","CTVA","CVS",
        "D","DFS","DG","DHI","DHR","DLR","DLTR","DOV","DPZ","DRI",
        "DTE","DUK","DVA","EFX","EG","EIX","EL","EMN","EMR","ES",
        "ESS","EW","EXC","EXPD","FAST","FDX","FIS","FISV","FLT","FMC",
        "FRC","FRT","GD","GL","HAS","HCA","HII","HLT","HOLX","HPQ",
        "HRL","HSIC","HST","HSY","HWM","ICE","IDXX","IEX","IFF","ILMN",
        "IP","IPG","IQV","IR","IRM","ISRG","IT","ITW","IVZ","J",
        "JBHT","JCI","JKHY","JNJ","JNPR","K","KEY","KHC","KIM",
        "KMB","KMI","KMX","KO","KR","L","LEN","LH","LIN","LKQ",
        "LNC","LNT","LUV","LYB","LYV","MAA","MAR","MAS","MCD","MCK",
        "MCO","MDLZ","MDT","MET","MHK","MKC","MKTX","MLM","MMC","MNST",
        "MO","MOS","MPC","MPW","MSCI","MSI","MTB","MTCH","MTD",
        "NDAQ","NEE","NEM","NI","NLOK","NLSN","NRG","NSC","NTAP",
        "NTRS","NUE","NVAX","NVR","NWL","NWS","NWSA",
        "OGN","OKE","OMC","ORLY","OTIS","PAYC","PAYX","PEG","PEP",
        "PFG","PGR","PH","PHM","PKG","PKI","PM","PNC","PNR","PNW",
        "POOL","PPG","PPL","PRU","PSX","PTC","PVH","PWR",
        "RE","REG","RF","RHI","RL","RMD","ROK","ROL","ROP","ROST",
        "RSG","SBAC","SBUX","SEE","SHW","SJM","SNA","SNPS","SO",
        "SPGI","SRE","STT","STX","STZ","SWK","T","TAP","TDG","TDY",
        "TEL","TER","TFC","TFX","TJX","TMO","TMUS","TPR","TRMB",
        "TROW","TRV","TSCO","TT","TXT","TYL","UDR","UHS","ULTA",
        "UNH","UNP","UPS","URI","USB","VFC","VLO","VMC","VNO","VNT",
        "VRSK","VRSN","VTR","VZ","WAB","WAT","WBA","WBD","WDC","WEC",
        "WHR","WM","WMB","WRB","WRK","WST","WTW","WY","XEL","XYL",
        "YUM","ZBH","ZBRA","ZION","ZTS",
    ]
    tickers.update(extras)

    # Strip any exchange suffixes, uppercase, remove empty
    clean = sorted({
        str(s).upper().replace(".","-").strip()
        for s in tickers
        if s and str(s).strip() and "." not in str(s).replace("-","")
    })

    OUT.write_text(json.dumps({"symbols": clean, "count": len(clean)}, indent=2))
    print(f"\nSaved {len(clean)} tickers -> {OUT}")

if __name__ == "__main__":
    main()
