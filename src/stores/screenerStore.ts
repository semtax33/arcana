import { makeAutoObservable } from "mobx";
import { contentCopy, primaryMenus, screenerMenus, utilityMenus } from "../data/menu";

export class ScreenerStore {
  activeMenuId = "quant-screener";
  query = "";
  locale: "KR" | "EN" = "KR";

  constructor() {
    makeAutoObservable(this);
  }

  get allMenus() {
    return [...primaryMenus, ...screenerMenus, ...utilityMenus];
  }

  get activeMenu() {
    return this.allMenus.find((menu) => menu.id === this.activeMenuId) ?? screenerMenus[0];
  }

  get activeContent() {
    return contentCopy[this.activeMenuId] ?? contentCopy["quant-screener"];
  }

  setActiveMenu(menuId: string) {
    this.activeMenuId = menuId;
  }

  setQuery(query: string) {
    this.query = query;
  }

  setLocale(locale: "KR" | "EN") {
    this.locale = locale;
  }
}

export const screenerStore = new ScreenerStore();
