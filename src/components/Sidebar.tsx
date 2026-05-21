import { observer } from "mobx-react-lite";
import { PanelLeftClose } from "lucide-react";
import { primaryMenus, screenerMenus, utilityMenus } from "../data/menu";
import type { ScreenerStore } from "../stores/screenerStore";

type SidebarProps = {
  store: ScreenerStore;
};

export const Sidebar = observer(({ store }: SidebarProps) => {
  return (
    <aside className="sidebar" aria-label="메인 메뉴">
      <div className="brand-row">
        <div className="brand-mark">G</div>
        <strong>ARCANA</strong>
        <button className="icon-button" type="button" aria-label="사이드바 접기">
          <PanelLeftClose size={16} />
        </button>
      </div>

      <nav className="menu-stack">
        <div className="menu-section">
          {primaryMenus.map((menu) => (
            <button
              className={`menu-item ${store.activeMenuId === menu.id ? "active" : ""}`}
              key={menu.id}
              type="button"
              onClick={() => store.setActiveMenu(menu.id)}
            >
              <menu.icon size={16} />
              <span>{menu.label}</span>
            </button>
          ))}
        </div>

        <div className="menu-section">
          <p className="menu-caption">퀀트</p>
          {screenerMenus.slice(0, 2).map((menu) => (
            <button
              className={`menu-item ${store.activeMenuId === menu.id ? "active" : ""}`}
              key={menu.id}
              type="button"
              onClick={() => store.setActiveMenu(menu.id)}
            >
              <menu.icon size={16} />
              <span>{menu.label}</span>
            </button>
          ))}
        </div>

        <div className="menu-section">
          <p className="menu-caption">종목 분석</p>
          {screenerMenus.slice(2).map((menu) => (
            <button
              className={`menu-item ${store.activeMenuId === menu.id ? "active" : ""}`}
              key={menu.id}
              type="button"
              onClick={() => store.setActiveMenu(menu.id)}
            >
              <menu.icon size={16} />
              <span>{menu.label}</span>
            </button>
          ))}
        </div>
      </nav>

      <div className="sidebar-footer">
        <div className="locale-switch" aria-label="언어 선택">
          <button
            className={store.locale === "KR" ? "selected" : ""}
            type="button"
            onClick={() => store.setLocale("KR")}
          >
            KR
          </button>
          <button
            className={store.locale === "EN" ? "selected" : ""}
            type="button"
            onClick={() => store.setLocale("EN")}
          >
            EN
          </button>
        </div>

        {utilityMenus.slice(1).map((menu) => (
          <button
            className={`menu-item ${store.activeMenuId === menu.id ? "active" : ""}`}
            key={menu.id}
            type="button"
            onClick={() => store.setActiveMenu(menu.id)}
          >
            <menu.icon size={16} />
            <span>{menu.label}</span>
          </button>
        ))}
      </div>
    </aside>
  );
});
