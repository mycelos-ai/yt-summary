import "./index.css";
import { Composition } from "remotion";
import { YtSummaryPromo } from "./Composition";
import { TOTAL_FRAMES } from "./lib/storyboard";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="YtSummaryPromo"
        component={YtSummaryPromo}
        durationInFrames={TOTAL_FRAMES}
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
