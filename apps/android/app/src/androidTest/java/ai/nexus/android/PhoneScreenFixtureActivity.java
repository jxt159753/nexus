package ai.nexus.android;

import android.app.Activity;
import android.content.*;
import android.graphics.*;
import android.os.*;
import android.view.*;

/** Canvas-only controls deliberately expose no text or click nodes. Test APK only. */
public class PhoneScreenFixtureActivity extends Activity {
    private Surface surface;
    private final BroadcastReceiver change = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (intent.getBooleanExtra("target", false)) surface.tap = Color.MAGENTA;
            else surface.counter++;
            surface.invalidate();
        }
    };
    @android.annotation.SuppressLint("UnspecifiedRegisterReceiverFlag") // Pre-33 branch has no exported flag requirement.
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        surface = new Surface();setContentView(surface);
        IntentFilter filter = new IntentFilter("ai.nexus.android.test.SCREEN_CHANGE");
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(change, filter, Context.RECEIVER_EXPORTED);
        else registerReceiver(change, filter);
    }
    @Override public void onDestroy() { unregisterReceiver(change);super.onDestroy(); }
    private class Surface extends View {
        final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        int tap=Color.LTGRAY, hold=Color.LTGRAY, swipe=Color.LTGRAY, counter=0;
        long down;float downX,downY;
        Surface() { super(PhoneScreenFixtureActivity.this);setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO); }
        void box(Canvas canvas,int top,int bottom,int color,String label) {
            paint.setColor(color);canvas.drawRect(120,top,900,bottom,paint);
            paint.setColor(Color.BLACK);paint.setTextSize(48);canvas.drawText(label,160,top+95,paint);
        }
        @Override protected void onDraw(Canvas canvas) {
            canvas.drawColor(Color.WHITE);
            box(canvas,240,580,tap,"Tap target");box(canvas,720,1050,hold,"Long press target");box(canvas,1200,1600,swipe,"Swipe left here");
            paint.setColor(Color.BLACK);paint.setTextSize(40);canvas.drawText("Refresh outside target: "+counter,80,1750,paint);
        }
        @Override public boolean onTouchEvent(MotionEvent event) {
            if(event.getAction()==MotionEvent.ACTION_DOWN) { down=SystemClock.elapsedRealtime();downX=event.getX();downY=event.getY();return true; }
            if(event.getAction()==MotionEvent.ACTION_UP) {
                if(downY>=240 && downY<=580) tap=Color.GREEN;
                if(downY>=720 && downY<=1050 && SystemClock.elapsedRealtime()-down>=500) hold=Color.YELLOW;
                if(downY>=1200 && downY<=1600 && downX-event.getX()>200) swipe=Color.BLUE;
                invalidate();return true;
            }
            return true;
        }
    }
}
