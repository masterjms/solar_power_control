// 1년 스케줄 지역 고르기(문제점 18번) — 손으로 치지 않고 전국 시·군 목록에서 고른다.
// 목록 밖의 현재 값(예: 단말 기본 "부산(기본 표)")은 맨 위에 "지금 값"으로 보여 주고 그대로 둘 수 있다.
import { CITY_GROUPS, City, cityOf } from "./cities";

export function CitySelect({ value, onPick, disabled }: { value: string; onPick: (c: City) => void; disabled?: boolean }) {
  const cur = value.trim();
  const known = cityOf(cur);
  return (
    <select className="inp" value={known ? known.label : cur ? "__cur" : ""} disabled={disabled} aria-label="지역(시·군)"
      onChange={(e) => {
        const c = cityOf(e.target.value);
        if (c) onPick(c);
      }}>
      {!cur && <option value="">시·군을 고른다</option>}
      {cur && !known && <option value="__cur">지금 값: {cur} (목록 밖)</option>}
      {CITY_GROUPS.map(([g, cs]) => (
        <optgroup key={g} label={g}>
          {cs.map((c) => <option key={c.label} value={c.label}>{c.label}</option>)}
        </optgroup>
      ))}
    </select>
  );
}
