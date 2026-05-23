import { observer } from "mobx-react-lite";
import { quickActions } from "../data/menu";
import type { QuantScreenerStore } from "../stores/quantScreenerStore";
import type { ScreenerStore } from "../stores/screenerStore";
import { IndustryAnalysisPage } from "./IndustryAnalysisPage";
import { QuantScreenerPage } from "./QuantScreenerPage";
import { StockAnalysisPage } from "./StockAnalysisPage";

type WorkspaceProps = {
  screenerStore: ScreenerStore;
  quantStore: QuantScreenerStore;
};

export const Workspace = observer(
  ({ screenerStore, quantStore }: WorkspaceProps) => {
    if (screenerStore.activeMenuId === "quant-screener") {
      return (
        <section className="workspace no-footer">
          <QuantScreenerPage store={quantStore} />
        </section>
      );
    }

    if (screenerStore.activeMenuId === "stock-analysis") {
      return (
        <section className="workspace no-footer">
          <StockAnalysisPage />
        </section>
      );
    }

    if (screenerStore.activeMenuId === "industry-analysis") {
      return (
        <section className="workspace no-footer">
          <IndustryAnalysisPage />
        </section>
      );
    }

    const content = screenerStore.activeContent;

    return (
      <section className="workspace">
        <header className="workspace-toolbar">
          <div className="toolbar-spacer" />
        </header>

        <div className="content-scroll">
          <section className="analysis-panel">
            <div className="copy-block">
              <p className="eyebrow">{content.eyebrow}</p>
              <h1>{content.title}</h1>
              <p>{content.summary}</p>
            </div>
            <div className="quick-grid">
              {quickActions.map((action) => (
                <button className="quick-action" key={action.id} type="button">
                  <action.icon size={17} />
                  <span>{action.label}</span>
                </button>
              ))}
            </div>
          </section>
        </div>
      </section>
    );
  },
);
