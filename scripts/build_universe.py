"""
One-shot script: download S&P 500 + S&P 400 + S&P 600 + NASDAQ 100 + curated extras
and write data/us_universe.json.  Run manually whenever you want to refresh.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT  = ROOT / "data" / "us_universe.json"


def _fetch_wiki_tickers(url: str, *cols: str) -> list[str]:
    """Try each column name in order until one matches."""
    try:
        import io
        import urllib.request
        import pandas as pd
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ticker-fetcher/1.0)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
        tables = pd.read_html(io.StringIO(html))
        for t in tables:
            for col in cols:
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

    print("Fetching S&P 600 small-cap...")
    tickers.update(_fetch_wiki_tickers(
        "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "Ticker", "Symbol"))

    print("Fetching NASDAQ-100...")
    tickers.update(_fetch_wiki_tickers(
        "https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", "Symbol"))

    # ── Curated extras ────────────────────────────────────────────────────────
    extras = [
        # Mega-cap tech
        "AAPL","MSFT","NVDA","AMD","META","GOOGL","GOOG","AMZN","TSLA",
        # Semis
        "AVGO","QCOM","MU","MRVL","AMAT","LRCX","KLAC","INTC","TXN",
        "ON","MCHP","WOLF","SMCI","ARM","SLAB","MPWR","NXPI","SWKS","QRVO",
        "CEVA","SYNA","CCMP","PDFS","AXTI","MTSI","AOSL","POWI","SITM",
        "ALGM","AEHR","FORM","UCTT","ACMR","LSCC","AMBA","PLAB","COHU",
        "ONTO","ICHR","AEIS","MKSI","DIOD","CRUS","RMBS","IXYS","POWER",
        # Cloud / SaaS
        "CRM","NOW","SNOW","PLTR","ORCL","ADBE","WDAY","TEAM","DDOG","NET",
        "ZS","PANW","CRWD","FTNT","CYBR","S","OKTA","HUBS","MDB","CFLT",
        "GTLB","APP","APPF","BILL","BRZE","ZI","DT","ESTC","FROG","PD",
        "TTD","PUBM","MGNI","IS","SPRK","AI","BBDC","RAMP","VCNX",
        # Fintech / crypto-adjacent
        "V","MA","PYPL","SQ","COIN","HOOD","SOFI","AFRM","NU","UPST",
        "MARA","RIOT","CLSK","HUT","CIFR","BTBT","IREN","WULF","CORZ",
        "SDIG","MIGI","BTDR","BSRT","HIVE","BITF",
        # Banks / finance
        "JPM","BAC","GS","MS","C","WFC","BX","BLK","SCHW","IBKR","RJF",
        "AXP","COF","DFS","SYF","ALLY","LC","OMF","CURO","NAVI","SLM",
        "TREE","ENVA","CACC","RM","QFIN","LKFN","CFFN","CVBF","HTLF",
        # Energy / commodities
        "XOM","CVX","COP","OXY","MRO","DVN","FANG","HES","EOG",
        "SLB","HAL","BKR","NOG","SM","AR","CTRA","RRC","EQT","CHRD",
        "VTLE","PDCE","REX","KRP","SBOW","CPE","PR","MTDR","BATL",
        # Healthcare / biotech
        "LLY","NVO","ABBV","MRK","PFE","BMY","AMGN","GILD","REGN","VRTX",
        "MRNA","BNTX","BIIB","ALNY","EXAS","RXRX","ACAD","ARWR","BEAM",
        "CRSP","EDIT","NTLA","FATE","KYMR","PRGO","JAZZ","INCY","SGEN",
        "RVMD","KRYS","CGON","DNLI","IMVT","NVCR","TGTX","ZNTL",
        "MDGL","RCUS","VERV","ARQT","VRNA","CRNX","ELVN","VRTX","XENE",
        "PRTA","ACLS","AVXL","AMAM","ADMA","CBLI","SENS","MTEM","CALT",
        "SLNO","BPMC","KPTI","KRUS","SNDX","YMAB","AGEN","AGIO","ANAB",
        "XNCR","NKTR","IOVA","FOLD","IDYA","ITOS","JANX","KALA","LQDA",
        # Consumer / retail
        "WMT","COST","TGT","HD","LOW","NKE","DIS","NFLX","UBER","LYFT",
        "DASH","BKNG","ABNB","EXPE","TRIP","YELP","GRUB","CART","DKNG",
        "PENN","CZR","MGM","LVS","WYNN","RCL","CCL","NCLH","DAL","UAL","AAL","LUV",
        "BOOT","BYND","EAT","ELF","GOLF","HBI","JACK","JWN","LOVE","PRPL",
        # AI / quantum / space
        "DELL","HPE","IONQ","RGTI","QUBT","LUNR","RKLB","SPCE","ASTS",
        "BBAI","SOUN","AISP","GFAI","BTAI","NLSP","BFLY","JOBY","ACHR",
        "EVTL","LILM","ARCHER","BLADE","KTTA","AIRO","SATL","MNTS","VORB",
        "VACQ","ASTR","SPIR","NAUT","DMTK","SABS","ATIP","SIEVERT",
        # EVs / clean energy
        "RIVN","LCID","NIO","LI","XPEV","ENPH","FSLR","RUN","NOVA",
        "F","GM","PLUG","BE","CHPT","BLNK","EVGO","PTRA","NKLA","WKHS",
        "ZEV","ARVL","SOLO","GOEV","RIDE","FUV","IDEX","AYRO","ELMS",
        "SUNW","MAXN","CSIQ","JKS","DQ","SOL","SPWR","STEM","BEEM",
        # Industrials / defense
        "GE","CAT","BA","RTX","LMT","NOC","DE","HON","GD","HII",
        "LHX","LDOS","SAIC","CACI","KTOS","AVAV","RDW","AMMO","DRS",
        "CACI","FLIR","SPCE","AJRD","BWXT","CW","HEICO","HEI","TDY",
        # Media / streaming / gaming
        "RBLX","EA","TTWO","SPOT","PARA","WBD","FOX","FOXA","NWSA",
        "TME","HUYA","IQ","BILI","DOYU","GRIN","SKLZ","NGMS","DDI",
        "GRVY","SLGG","MAPS","MSGM","DKNG","PENN","RSI","EBET","GENI",
        # Meme / WSB / high short interest
        "GME","AMC","BBBY","CLOV","SNDL","NKLA","WKHS","RIDE","HYLN",
        "PROG","SPRT","BBIG","EXPR","KOSS","NAKD","NNDM","CTRM","SHIP",
        "MVIS","ABST","TPVG","WISH","PAYA","LMND","ROOT","ASAN","BASE",
        # Small/mid cap momentum (semiconductors + tech)
        "SMTC","IIVI","COHU","FORM","MXL","AMBA","PLAB","ICHR","MKSI",
        "DIOD","CRUS","RMBS","PDFS","AXTI","MTSI","AOSL","POWI","SITM",
        "ALGM","AEHR","UCTT","ACMR","LSCC","ONTO","AEIS","IXYS",
        # REITs
        "O","AMT","PLD","WELL","SPG","EQR","AVB","PSA","EXR","DLR",
        "IRM","SBAC","CCI","VICI","GLPI","STAG","COLD","IIPR",
        "PTON","GOOD","ACRE","RITM","LADR","CLNC","GPMT","TRTX",
        # Popular penny/low-priced plays for AH/PM
        "BBAI","SOUN","OPEN","ACHR","PLUG","MARA","RIOT","CLSK","HOOD",
        "SOFI","CLOV","SPCE","WKHS","NKLA","SNDL","NIO","XPEV","LCID",
        "RIVN","IONQ","RGTI","QUBT","LUNR","ASTS","RKLB","BTBT","IREN",
        "CIFR","HUT","BBBY","NWL","OPK","KEEL","ACMR","BKSY","MULN",
        "VINC","INDO","SMFL","PHIO","SHOT","IDT","EVTL","JOBY","LILM",
        "VOXX","COVA","PSHG","AEYE","MNMD","HCMC","MMAT","TRCH","CTRM",
        "GFAI","NXTP","AUVI","ILUS","BFRI","HYMC","TPVG","SVNA","OPAD",
        "EBON","BTCM","BTOG","PDSB","HCTI","CODA","LAKE","FGEN","NUVB",
        "TLGA","GXII","GETY","BRPM","SLGN","NKGN","SIGA","MNKD","PRTK",
        "CTXR","PIXY","BFIN","CLFD","CVLY","EDTX","ELEV","FFIE","FWBI",
        # S&P 500 high-liquidity (full complement)
        "AAON","ACM","AES","AIG","AIZ","AJG","AKAM","ALB","ALGN","ALK",
        "ALL","ALLE","ANET","AON","APA","APD","APH","APTV","ARE","ATO",
        "AWK","AZO","BBY","BDX","BEN","BIO","BK","BMRN","BR","BRK-B",
        "BRO","BSX","BXP","CB","CBOE","CBRE","CDW","CE","CF","CHD",
        "CHRW","CHTR","CI","CINF","CLX","CMCSA","CMS","CNC","CNP",
        "COO","CPRT","CPT","CSX","CTAS","CTLT","CTSH","CTVA","CVS",
        "D","DFS","DG","DHI","DHR","DLR","DLTR","DOV","DPZ","DRI",
        "DTE","DUK","DVA","EFX","EG","EIX","EL","EMN","EMR","ES",
        "ESS","EW","EXC","EXPD","FAST","FDX","FIS","FISV","FLT","FMC",
        "FRC","FRT","GD","GL","HAS","HCA","HLT","HOLX","HPQ",
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
        # Recent hot tickers / Reddit momentum
        "FFIE","MVIS","CODI","GOEV","NNDM","PRTY","ZAPP","RCAT","SEAT",
        "SKYH","MGRM","LGVN","MNTK","NVTS","NXT","COCO","REAX","BKFC",
        "ACXP","SXTP","LIQT","ZFOX","AEAC","FWAC","AHCO","DNUT","TASK",
        "XPOF","BARK","PAYO","BRLT","MNTV","KARO","GLBE","FIGS","RENT",
        "AMPL","WEJO","OUST","LSEA","GETY","HPNN","DPSI","FLGC","DRUG",
        "FRST","HIMS","EVER","LPSN","PTON","BMBL","DUOL","COUR","UDMY",
        "PWSC","PAYO","ALKT","NRDS","MNDY","KVYO","INSTC","KLAR","RDDT",
    ]
    tickers.update(extras)

    # Strip dots (use hyphen for BRK-B style), uppercase, remove empty
    clean = sorted({
        str(s).upper().replace(".", "-").strip()
        for s in tickers
        if s and str(s).strip() and "." not in str(s).replace("-", "")
    })

    OUT.write_text(json.dumps({"symbols": clean, "count": len(clean)}, indent=2))
    print(f"\nSaved {len(clean)} tickers -> {OUT}")


if __name__ == "__main__":
    main()
