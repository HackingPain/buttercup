/**
 * Sample Java project for integration testing the Tree-sitter parsing pipeline.
 * Contains classes, interfaces, enums, records, and annotations.
 */

import java.util.List;
import java.util.ArrayList;

enum Priority {
    LOW,
    MEDIUM,
    HIGH,
    CRITICAL
}

interface Validator {
    boolean validate(String input);
    String getErrorMessage();
}

class StringValidator implements Validator {
    private int maxLength;
    private String pattern;

    public StringValidator(int maxLength) {
        this.maxLength = maxLength;
        this.pattern = ".*";
    }

    public boolean validate(String input) {
        if (input == null) {
            return false;
        }
        if (input.length() > maxLength) {
            return false;
        }
        return input.matches(pattern);
    }

    public String getErrorMessage() {
        return "Validation failed: input exceeds max length " + maxLength;
    }

    private boolean checkPattern(String input) {
        return input.matches(pattern);
    }
}

class DataProcessor {
    private List<String> items;
    private Validator validator;

    public DataProcessor(Validator validator) {
        this.items = new ArrayList<>();
        this.validator = validator;
    }

    public void addItem(String item) {
        if (validator.validate(item)) {
            items.add(item);
        }
    }

    public int processAll() {
        int count = 0;
        for (String item : items) {
            if (processItem(item)) {
                count++;
            }
        }
        return count;
    }

    private boolean processItem(String item) {
        return item != null && !item.isEmpty();
    }

    public List<String> getItems() {
        return new ArrayList<>(items);
    }
}

public class sample_java_project {
    public static void main(String[] args) {
        StringValidator validator = new StringValidator(100);
        DataProcessor processor = new DataProcessor(validator);
        processor.addItem("hello");
        processor.addItem("world");
        System.out.println("Processed: " + processor.processAll());
    }
}
