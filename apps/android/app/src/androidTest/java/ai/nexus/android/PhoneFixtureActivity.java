package ai.nexus.android;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.widget.*;

/** Standalone test APK components use only the framework, not the target APK's Kotlin runtime. */
public class PhoneFixtureActivity extends Activity {
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        Uri uri = Uri.parse("content://ai.nexus.android.test.documents/tree/root");
        grantUriPermission("ai.nexus.android", uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
        LinearLayout column = new LinearLayout(this); column.setOrientation(LinearLayout.VERTICAL);
        TextView title = new TextView(this); title.setText("Phone test initial"); title.setTextSize(24);
        StringBuilder description = new StringBuilder(); for (int i=0; i<299; i++) description.append('x');
        title.setContentDescription(description.append("🙂tail").toString());
        column.addView(title);
        Button click = new Button(this); click.setAllCaps(false); click.setText("Phone test click");
        click.setOnClickListener(view -> title.setText("Phone test clicked")); column.addView(click);
        EditText input = new EditText(this); input.setHint("Phone test input"); input.setContentDescription("Phone test input"); column.addView(input);
        EditText password = new EditText(this); password.setInputType(129); password.setText("private-password");
        password.setContentDescription("private-password"); column.addView(password);
        ScrollView scroll = new ScrollView(this); LinearLayout rows = new LinearLayout(this); rows.setOrientation(LinearLayout.VERTICAL);
        for (int i=0; i<61; i++) {
            TextView row = new TextView(this); row.setText("Phone row " + i); row.setTextSize(24); row.setPadding(10,20,10,20); rows.addView(row);
        }
        scroll.addView(rows); column.addView(scroll); setContentView(column);
    }
}
