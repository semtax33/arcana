import {
  Bell,
  BookOpen,
  Globe2,
  Languages,
  LogOut,
  PieChart,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Star,
  UserRound,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type MenuItem = {
  id: string;
  label: string;
  icon: LucideIcon;
  group?: string;
};

export const primaryMenus: MenuItem[] = [];

export const screenerMenus: MenuItem[] = [
  {
    id: "quant-screener",
    label: "퀀트 스크리너",
    icon: SlidersHorizontal,
    group: "퀀트",
  },
  {
    id: "screen-score",
    label: "스크린 스코어",
    icon: Star,
    group: "퀀트",
  },
  {
    id: "stock-analysis",
    label: "종목분석",
    icon: PieChart,
    group: "종목분석",
  },
];

export const utilityMenus: MenuItem[] = [
  { id: "language", label: "Language", icon: Languages },
  { id: "settings", label: "설정", icon: Settings },
  { id: "profile", label: "프로필", icon: UserRound },
  { id: "logout", label: "로그아웃", icon: LogOut },
];

export const contentCopy: Record<
  string,
  { title: string; eyebrow: string; summary: string }
> = {
  "quant-screener": {
    title: "퀀트 스크리너",
    eyebrow: "조건 기반 종목 필터링",
    summary: "시장, 산업, 팩터 조건을 조합해 투자 후보군을 빠르게 정리합니다.",
  },
  "stock-analysis": {
    title: "종목 분석",
    eyebrow: "기업 단위 리서치",
    summary: "재무, 밸류에이션, 가격 흐름을 종목별로 정리합니다.",
  },
  "screen-score": {
    title: "스크린 스코어",
    eyebrow: "팩터 점수 비교",
    summary: "각 종목의 핵심 팩터 점수를 한눈에 비교합니다.",
  },
  settings: {
    title: "설정",
    eyebrow: "환경 설정",
    summary: "알림, 기본 시장, 화면 옵션을 조정합니다.",
  },
};

export const quickActions = [
  { id: "notify", label: "보고서", icon: Bell },
  { id: "clear", label: "가이드", icon: BookOpen },
  { id: "global", label: "글로벌", icon: Globe2 },
  { id: "spark", label: "추천", icon: Sparkles },
];
