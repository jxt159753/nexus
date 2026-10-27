package ai.nexus.android;

import android.database.Cursor;
import android.database.MatrixCursor;
import android.os.CancellationSignal;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsProvider;
import android.provider.DocumentsContract.Document;
import android.provider.DocumentsContract.Root;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;

/** Real URI grants and descriptor I/O against isolated test data; absent from the shipping APK. */
public class PhoneFixtureDocuments extends DocumentsProvider {
    private File root() { return new File(getContext().getFilesDir(), "phone-documents"); }
    @Override public boolean onCreate() {
        root().mkdirs(); File initial = new File(root(), "existing.txt");
        try { if (!initial.exists()) Files.write(initial.toPath(), "old phone content".getBytes(StandardCharsets.UTF_8)); }
        catch (IOException e) { throw new IllegalStateException(e); }
        return true;
    }
    private File file(String id) {
        if (!id.equals("root") && !id.startsWith("root/")) throw new IllegalArgumentException(id);
        File result = id.equals("root") ? root() : new File(root(), id.substring(5));
        try {
            String path = result.getCanonicalPath(), base = root().getCanonicalPath();
            if (!path.equals(base) && !path.startsWith(base + File.separator)) throw new IllegalArgumentException(id);
        } catch (IOException e) { throw new IllegalStateException(e); }
        return result;
    }
    private static final String[] COLUMNS = {Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
        Document.COLUMN_MIME_TYPE, Document.COLUMN_FLAGS, Document.COLUMN_SIZE};
    private void row(MatrixCursor cursor, String id) {
        File f = file(id); if (!f.exists()) throw new IllegalArgumentException(id);
        Map<String,Object> values = new HashMap<>();
        values.put(Document.COLUMN_DOCUMENT_ID,id); values.put(Document.COLUMN_DISPLAY_NAME,f.getName());
        values.put(Document.COLUMN_MIME_TYPE,f.isDirectory() ? Document.MIME_TYPE_DIR : "text/plain");
        values.put(Document.COLUMN_FLAGS,f.isDirectory() ? Document.FLAG_DIR_SUPPORTS_CREATE
            : Document.FLAG_SUPPORTS_WRITE | Document.FLAG_SUPPORTS_RENAME | Document.FLAG_SUPPORTS_DELETE);
        values.put(Document.COLUMN_SIZE,f.length()); add(cursor,values);
    }
    private void add(MatrixCursor cursor, Map<String,Object> values) {
        Object[] row = new Object[cursor.getColumnCount()]; String[] columns = cursor.getColumnNames();
        for (int i=0; i<row.length; i++) row[i]=values.get(columns[i]); cursor.addRow(row);
    }
    @Override public Cursor queryRoots(String[] projection) {
        MatrixCursor cursor = new MatrixCursor(projection != null ? projection : new String[] {Root.COLUMN_ROOT_ID,Root.COLUMN_DOCUMENT_ID,Root.COLUMN_TITLE,Root.COLUMN_FLAGS});
        Map<String,Object> values = new HashMap<>(); values.put(Root.COLUMN_ROOT_ID,"root"); values.put(Root.COLUMN_DOCUMENT_ID,"root");
        values.put(Root.COLUMN_TITLE,"Phone fixture"); values.put(Root.COLUMN_FLAGS,Root.FLAG_SUPPORTS_CREATE); add(cursor,values); return cursor;
    }
    @Override public Cursor queryDocument(String id, String[] projection) {
        MatrixCursor cursor = new MatrixCursor(projection != null ? projection : COLUMNS); row(cursor,id); return cursor;
    }
    @Override public Cursor queryChildDocuments(String id, String[] projection, String order) {
        MatrixCursor cursor = new MatrixCursor(projection != null ? projection : COLUMNS);
        File[] children = Objects.requireNonNull(file(id).listFiles()); Arrays.sort(children,Comparator.comparing(File::getName));
        for (File child : children) row(cursor,id + "/" + child.getName()); return cursor;
    }
    @Override public ParcelFileDescriptor openDocument(String id, String mode, CancellationSignal signal) throws FileNotFoundException {
        return ParcelFileDescriptor.open(file(id),ParcelFileDescriptor.parseMode(mode));
    }
    private void name(String value) {
        if (value.contains("/") || value.equals(".") || value.equals("..")) throw new IllegalArgumentException(value);
    }
    @Override public String createDocument(String parent, String mime, String displayName) {
        name(displayName); String id = parent + "/" + displayName;
        try { if (!file(id).createNewFile()) throw new IllegalStateException(id); }
        catch (IOException e) { throw new IllegalStateException(e); } return id;
    }
    @Override public String renameDocument(String id, String displayName) {
        name(displayName); String result = id.substring(0,id.lastIndexOf('/')) + "/" + displayName;
        if (file(result).exists() || !file(id).renameTo(file(result))) throw new IllegalStateException(result); return result;
    }
    @Override public void deleteDocument(String id) {
        if (id.equals("root") || !file(id).delete()) throw new IllegalStateException(id);
    }
    @Override public boolean isChildDocument(String parent, String child) {
        try { return file(child).getCanonicalPath().startsWith(file(parent).getCanonicalPath() + File.separator); }
        catch (IOException e) { throw new IllegalStateException(e); }
    }
}
